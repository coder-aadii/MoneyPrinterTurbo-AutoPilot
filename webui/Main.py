import hashlib
import html
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
import zipfile
from io import BytesIO
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import requests
import streamlit as st
from loguru import logger
from streamlit_tour import Tour

# WebUI 作为独立入口运行时，需要让项目根目录优先于第三方依赖，
# 避免依赖中的同名 app 包遮蔽 MoneyPrinterTurbo 自己的 app 包。
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

from app.config import config
from app.models import const
from app.models.llm_provider import (
    DEFAULT_LLM_PROVIDER_ID,
    LLM_PROVIDER_REGISTRY,
    get_llm_provider,
    normalize_provider_override,
)
from app.models.schema import (
    ImageMotion,
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoFitMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import bgm as bgm_service
from app.services import material_upload as material_upload_service
from app.services import (
    autopilot as autopilot_service,
    cache_manager,
    channel_profiles,
    llm,
    script_quality,
    loomloom,
    material,
    metaso_minimax,
    muapi,
    ofox,
    subtitle,
    video,
    volcengine_seedance,
    voice,
    webui_task,
)
from app.services import elevenlabs_music as elevenlabs_music_service
from app.services import sonilo as sonilo_service
from app.services import visual_review
from app.services import generation_settings as generation_settings_service
from app.services import hindi_script
from app.services import visual_style as visual_style_service
from app.services import source_research as source_research_service
from app.services import state as sm
from app.services import task as tm
from app.services import version_checker
from webui.autopilot_view import migrate_create_video_style
from app.utils.logging_utils import (
    LOG_LEVELS,
    configure_terminal_logger,
    get_logging_options,
)
from app.utils import utils
from app.utils.request_logging import trace_http_call

VISUAL_STYLE_LABELS = {
    "savage_animated_explainer": "Savage Animated Explainer",
    "relevant_stock": "Relevant Stock",
    "asmr_cutting": "ASMR Cutting",
    "auto": "AUTO",
}
VISUAL_STYLE_VALUES = {label: value for value, label in VISUAL_STYLE_LABELS.items()}

st.set_page_config(
    page_title="MoneyPrinterTurbo",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": "# MoneyPrinterTurbo\nSimply provide a topic or keyword for a video, and it will "
        "automatically generate the video copy, video materials, video subtitles, "
        "and video background music before synthesizing a high-definition short "
        "video.\n\nhttps://github.com/harry0703/MoneyPrinterTurbo",
    },
)


# Streamlit 1.59 会在页面右上角默认展示 Deploy、skills nudge 等平台入口。
# MoneyPrinterTurbo 是面向终端用户的本地工具，这些入口会造成顶部大块空白，
# 也会让新用户误以为需要安装额外组件。这里统一隐藏 Streamlit 平台工具栏，
# 并压缩主容器顶部留白，只保留项目自己的标题、语言选择和业务设置区域。
style_file = Path(__file__).with_name("styles.css")
streamlit_style = f"<style>{style_file.read_text(encoding='utf-8')}</style>"
st.markdown(streamlit_style, unsafe_allow_html=True)
# 定义资源目录
font_dir = utils.font_dir()
song_dir = utils.song_dir()
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
# 语言列表必须在会话状态初始化前可用，首次访问时才能把浏览器 locale 映射到
# 项目真正支持的语言；自动识别结果只进入当前会话，不修改全局配置。
locales = utils.load_locales(i18n_dir)
DEFAULT_CHATTERBOX_BASE_URL = "http://127.0.0.1:4123/v1"
DEFAULT_CHATTERBOX_MODEL = "chatterbox"
DEFAULT_CHATTERBOX_VOICES = ["default-Female"]
DEFAULT_KOKORO_BASE_URL = "http://127.0.0.1:8880/v1"
DEFAULT_KOKORO_MODEL = "kokoro"
# empty = ask the server for its voice list (GET {base_url}/audio/voices)
DEFAULT_KOKORO_VOICES: list[str] = []
DEFAULT_VOXCPM_BASE_URL = voice.VOXCPM_DEFAULT_BASE_URL
DEFAULT_VOXCPM_VOICE = voice.VOXCPM_DEFAULT_VOICE
VOXCPM_REFERENCE_AUDIO_SESSION_KEY = "voxcpm_reference_audio"
VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY = "voxcpm_reference_audio_error"
VOXCPM_PROMPT_AUDIO_SESSION_KEY = "voxcpm_prompt_audio"
VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY = "voxcpm_prompt_audio_error"
VOXCPM_PROMPT_TEXT_SESSION_KEY = "voxcpm_prompt_text_input"
VOXCPM_HIGH_FIDELITY_SESSION_KEY = "voxcpm_high_fidelity_enabled"
VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY = "voxcpm_separate_prompt_audio_enabled"
VOXCPM_PROMPT_EXAMPLE_MODE_SESSION_KEY = "voxcpm_prompt_example_mode"
ONBOARDING_TOUR_KEY = "mpt-onboarding-v1"
CUSTOM_LLM_ENDPOINT_ID = "custom"
VOICE_MODE_TTS = "tts"
VOICE_MODE_UPLOAD = "upload"
VOICE_MODE_NONE = "none"
LOOMLOOM_MAX_POLL_FAILURES = 5
# WebUI 按素材能力分组展示视频来源，但底层仍保存原有 video_source 值。
# AI 视频组与设置页共用同一业务顺序：合作服务商优先，并按秘塔、OFox、
# 胜算云、火山引擎排列；其余服务随后展示。这样两个入口的顺序一致，同时
# 不改变 config.toml、历史任务和 API 请求中的字段语义，旧用户无需迁移配置。
VIDEO_SOURCE_GROUPS = {
    "stock_video": ("pexels", "pixabay", "coverr"),
    "ai_video": (
        "metaso_minimax",
        "ofox",
        "loomloom",
        "volcengine_seedance",
        "wavespeed",
        "muapi",
    ),
    "ai_image": ("openai_image",),
    "local": ("local",),
}
# Upload-Post 的 API Key 与发布用户分别在两个页面管理，并且发布用户名称
# 不等于登录邮箱。集中维护入口可以避免多语言文案各自硬编码 URL 后发生偏差，
# 也方便用户从 WebUI 直接完成首次配置和后续账号维护。
UPLOAD_POST_API_KEYS_URL = "https://app.upload-post.com/api-keys"
UPLOAD_POST_MANAGE_USERS_URL = "https://app.upload-post.com/manage-users"
# 素材设置与视频来源说明共用推广入口，避免两个位置的链接参数不一致。
OFOX_REFERRAL_URL = (
    "https://ofox.ai/?utm_source=github"
    "&utm_medium=sponsorship&utm_content=moneyprinterturbo"
)
# “默认”是 WebUI 专用哨兵，不会写入 config.toml，也不会传给 FFmpeg。
# 后端在 video_codec 未配置时继续采用稳定的 libx264；单独保留该哨兵可以区分
# “跟随项目默认策略”和“用户明确固定 libx264”，便于未来安全调整默认策略。
DEFAULT_VIDEO_CODEC_OPTION = "__default__"
# LoomLoom 的能力接口只返回模型 ID 和展示名，不提供价格。这里仅维护用户确认过
# 的参考价，用于帮助选择模型；最终费用按实际模型调用结算。别名同时覆盖
# 当前展示名和常见模型 ID，未收录的新模型会自然返回空价格，不影响选择或报价。
LOOMLOOM_VIDEO_MODEL_PRICES = (
    (("veo31fast", "googleveo31fastpreview"), "￥0.700/秒", "￥0.700/秒"),
    (
        (
            "通义万相22图生视频fastlora",
            "通义万相22文生视频fastlora",
            "tongyiwanxiang22i2vfastlora",
            "tongyiwanxiang22t2vfastlora",
            "wanx22i2vfastlora",
            "wanx22t2vfastlora",
        ),
        "￥0.350–0.770/条",
        "￥0.350/条（480P）；￥0.770/条（720P）",
    ),
    (("即梦30文生视频720p", "jimeng30t2v720p"), "￥0.230/秒", "￥0.230/秒"),
    (("即梦30pro视频", "jimeng30pro视频", "jimeng30provideo"), "￥1.000/秒", "￥1.000/秒"),
    (("veo3", "googleveo3"), "￥1.400/秒", "￥1.400/秒"),
    (("veo31", "googleveo31"), "￥1.400/秒", "￥1.400/秒"),
    (
        ("klingv2", "可灵v2"),
        "￥10.00–20.00/条",
        "￥10.00/条（5 秒）；￥20.00/条（10 秒）",
    ),
    (
        ("klingv21master", "可灵v21master"),
        "￥10.00–20.00/条",
        "￥10.00/条（5 秒）；￥20.00/条（10 秒）",
    ),
    (
        ("viduq3pro",),
        "￥0.440–1.000/秒",
        "￥0.440/秒（540P）；￥0.940/秒（720P）；￥1.000/秒（1080P）",
    ),
)
DEFAULT_SUBTITLE_SETTINGS = {
    "subtitle_enabled": True,
    "font_name": "NotoSansDevanagari-VF.ttf",
    "subtitle_position": "bottom",
    "subtitle_display_mode": "sentence",
    "subtitle_animation": "none",
    "custom_position": 70.0,
    "text_fore_color": "#FFFFFF",
    "font_size": 60,
    "stroke_color": "#000000",
    "stroke_width": 1.5,
    "subtitle_background_enabled": False,
    "subtitle_background_color": "#000000",
    "rounded_subtitle_background": False,
}
LOCAL_MATERIAL_EXTENSIONS = set(material_upload_service.SUPPORTED_MATERIAL_EXTENSIONS)
CUSTOM_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_FINAL_VIDEO_PATTERN = re.compile(
    r"^final-(?P<index>\d+)\.(?P<extension>mp4|mov|mkv|webm)$",
    re.IGNORECASE,
)
_DOWNLOAD_FILENAME_INVALID_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_FILENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in range(1, 10)
    }
    # Win32 还会把 Latin-1 上标数字 ¹、²、³ 识别为设备编号。虽然这类主题
    # 很少见，但仍会导致 Windows 下载失败，因此与普通数字保留名统一处理。
    | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in ("¹", "²", "³")
    }
)
_RUNTIME_CONFIG_SECTIONS = {
    "app": config.app,
    "azure": config.azure,
    "chatterbox": config.chatterbox,
    "kokoro": config.kokoro,
    "elevenlabs": config.elevenlabs,
    "minimax_tts": config.minimax_tts,
    "siliconflow": config.siliconflow,
    "fish_audio": config.fish_audio,
    "voxcpm": config.voxcpm,
    "ui": config.ui,
}
# 设置预设与密钥备份使用各自的文件标识。导入时先校验 schema 和版本，
# 避免把任务记录、config.toml 或其它 JSON 误当成本功能的导出文件。
SETTINGS_PRESET_SCHEMA = "moneyprinterturbo.settings-preset"
SETTINGS_PRESET_VERSION = 1
SETTINGS_PRESET_FILE_NAME = "moneyprinterturbo-settings.json"
KEY_BACKUP_SCHEMA = "moneyprinterturbo.key-backup"
KEY_BACKUP_VERSION = 1
KEY_BACKUP_FILE_NAME = "moneyprinterturbo-keys.json"
# Export files contain only settings or credentials, not media. Reject oversized
# uploads before decoding and parsing them in the Streamlit process.
MAX_SETTINGS_TRANSFER_BYTES = 2 * 1024 * 1024
# 预设只描述生成参数。素材、配音和配乐都是本机文件路径，预设通常要在另一台
# 机器或另一个容器里导入，带上这些路径只会指向不存在的文件。
PRESET_EXCLUDED_PARAM_KEYS = frozenset(
    {
        "video_materials",
        "custom_audio_file",
        "bgm_file",
    }
)
# 密钥按配置项名称后缀识别。新增 Provider 只要沿用现有命名，就会自动进入
# 备份，不需要再维护第二份密钥清单。
CREDENTIAL_KEY_SUFFIXES = (
    "api_key",
    "api_keys",
    "api_token",
    "access_key",
    "secret_key",
    "speech_key",
)
# 只恢复密钥而不恢复配套配置项时，凭据仍然不可用。这些配套项与密钥一起备份。
CREDENTIAL_COMPANION_KEYS = {
    # Azure 语音必须同时知道区域。
    "azure": ("speech_region",),
    # Provider 的额外字段由 Registry 声明，例如 Cloudflare AI Gateway 的
    # Account ID 和 Gateway ID。只恢复 API Key 而丢掉这些字段时，换到另一台
    # 机器后该 Provider 仍然无法调用。从 Registry 读取可以让以后新增的
    # Provider 自动进入备份，不需要在这里维护第二份字段清单。
    "app": tuple(
        provider.config_key(field.config_suffix)
        for provider in LLM_PROVIDER_REGISTRY
        for field in provider.extra_fields
    ),
}

NON_LLM_COMPANION_KEYS = {
    "app": ("upload_post_username",)
}
# 同一个密钥在不同面板可能使用各自的控件 key：音频面板直接编辑 Gemini 和
# MiMo 的 LLM 密钥。恢复备份时必须清除每一个别名，否则遗留的旧值
# 会在下一次 rerun 覆盖刚刚恢复的密钥。
CREDENTIAL_WIDGET_STATE_ALIASES = {
    ("app", "gemini_api_key"): ("gemini_tts_api_key_input",),
    ("app", "mimo_api_key"): ("mimo_tts_api_key_input",),
}
# ui 分区只保存界面偏好，不含任何凭据，备份时整体跳过。
KEY_BACKUP_EXCLUDED_SECTIONS = frozenset({"ui"})


# -----------------------------------------------------------------------------
# 启动配置、会话状态与本地化
# -----------------------------------------------------------------------------


def _set_runtime_config(section_name, key, value):
    """
    更新 WebUI 配置，但不等待正在生成视频的后台任务。

    后台任务结束前，配置层只保留同一配置项的最新值；任务释放配置锁时会自动
    应用并保存。页面控件值仍由 Streamlit session_state 维护，因此暂存期间的
    rerun 不会把用户刚输入的内容重置为旧配置。
    """
    config_section = _RUNTIME_CONFIG_SECTIONS[section_name]
    updated = config.update_config_nonblocking(config_section, key, value)
    if not updated:
        logger.debug(f"deferred WebUI config update: section={section_name}, key={key}")
    return updated


def _delete_runtime_config(section_name, key):
    """删除 WebUI 配置项；后台任务占用配置时延后执行。"""
    config_section = _RUNTIME_CONFIG_SECTIONS[section_name]
    deleted = config.delete_config_nonblocking(config_section, key)
    if not deleted:
        logger.debug(f"deferred WebUI config delete: section={section_name}, key={key}")
    return deleted


def _save_runtime_config():
    """请求保存 WebUI 配置；后台任务占用配置时立即返回。"""
    saved = config.try_save_config()
    if not saved:
        logger.debug("deferred WebUI config save until active task completes")
    return saved


def _saved_ui_choice(key, options, default, section=None):
    """读取一个持久化选择，并把旧配置或手工编辑的非法值降级为默认值。"""
    options = list(options)
    section = config.ui if section is None else section
    saved = section.get(key, default)
    numeric_default = isinstance(default, (int, float)) and not isinstance(
        default, bool
    )
    # bool 是 int 的子类，``True == 1``。手工把数值选项写成 TOML
    # 布尔值时必须拒绝，不能让它伪装成第一个数值 option。
    if numeric_default and isinstance(saved, bool):
        return default
    for option in options:
        if saved == option:
            # 返回 options 中的真实值，顺便把 TOML 1.0 等价归一化为
            # 整数选项 1，避免下游参数类型随配置写法漂移。
            return option

    # TOML 中的数值通常保留原类型；仍兼容用户手工写成字符串的情况。
    if numeric_default and isinstance(saved, str):
        try:
            converted = type(default)(saved)
        except (TypeError, ValueError):
            converted = None
        for option in options:
            if converted == option:
                return option
    return default


def _saved_ui_number(key, default, minimum, maximum, number_type=float):
    """读取并限幅持久化数值，避免非法配置破坏 Streamlit slider。"""
    try:
        saved = config.ui.get(key, default)
        if isinstance(saved, bool):
            raise ValueError("boolean is not a numeric setting")
        value = number_type(saved)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite value")
    except (TypeError, ValueError, OverflowError):
        value = default
    return min(maximum, max(minimum, value))


def _saved_ui_bool(key, default):
    """兼容 TOML 布尔值和常见手工字符串，拒绝含义不明的旧值。"""
    value = config.ui.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _saved_ui_color(key, default):
    """只把标准六位十六进制颜色传给 Streamlit color picker。"""
    value = str(config.ui.get(key, default) or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    return default


def _saved_ui_text(key, default="", max_length=None):
    """读取持久化文本并遵守对应 WebUI 控件的长度上限。"""
    value = str(config.ui.get(key, default) or default)
    if max_length is not None:
        value = value[:max_length]
    return value


def _run_llm_read_operation(operation_name, operation):
    """
    使用稳定的当前 LLM 配置执行只读请求，并避免等待视频生成任务。

    能立即取得配置锁时继续沿用原来的互斥保护；锁已被后台视频任务持有时，
    全局配置在任务结束前不会发生变化，因此可以安全复制当前配置，并叠加页面
    尚未落盘的 Provider、模型和密钥。这样新文案使用界面中的最新选择，同时
    不会改变正在生成的视频任务。
    """
    with config.try_runtime_config_lock() as lock_acquired:
        # 配置层在复制全局值和叠加待更新值期间持有队列锁，因此快照只能看到
        # 更新前或更新后的完整状态，不会混用两组 Provider 参数。
        app_config_snapshot = config.snapshot_config_with_pending(config.app)
        if lock_acquired:
            return operation(app_config_snapshot)

    logger.info(
        f"run read-only LLM operation with active task configuration: "
        f"operation={operation_name}"
    )
    return operation(app_config_snapshot)


def _parse_chatterbox_voices(voices):
    # Chatterbox 是自托管服务，音色列表由用户在 WebUI 中手动输入。
    # 这里统一兼容 TOML 数组和输入框里的逗号分隔字符串，避免下拉框、
    # 试听按钮和后续生成流程使用不同格式导致状态不一致。
    if isinstance(voices, str):
        return [v.strip() for v in voices.split(",") if v.strip()]
    return [str(v).strip() for v in voices or [] if str(v).strip()]


def _sync_chatterbox_config_from_session_state():
    # Streamlit 的按钮会触发整页 rerun，而 Chatterbox 配置输入框位于
    # “试听语音合成”按钮之后。如果试听时只读取 config.chatterbox，可能拿不到
    # 用户刚在输入框里填入的 base_url/model/voices。先从 session_state 同步一次，
    # 可以保证按钮逻辑和输入框显示逻辑使用同一份最新配置。
    _set_runtime_config(
        "chatterbox",
        "base_url",
        (
            st.session_state.get(
                "chatterbox_base_url_input",
                config.chatterbox.get("base_url") or DEFAULT_CHATTERBOX_BASE_URL,
            )
            or ""
        ).strip(),
    )
    _set_runtime_config(
        "chatterbox",
        "api_key",
        st.session_state.get(
            "chatterbox_api_key_input", config.chatterbox.get("api_key", "")
        ),
    )
    _set_runtime_config(
        "chatterbox",
        "model_id",
        (
            st.session_state.get(
                "chatterbox_model_input",
                config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
            )
            or DEFAULT_CHATTERBOX_MODEL
        ).strip(),
    )
    _set_runtime_config(
        "chatterbox",
        "voices",
        _parse_chatterbox_voices(
            st.session_state.get(
                "chatterbox_voices_input",
                config.chatterbox.get("voices") or DEFAULT_CHATTERBOX_VOICES,
            )
        ),
    )


def _sync_kokoro_config_from_session_state():
    # 音色目录先于设置输入框渲染，先同步浏览器状态，确保本次 rerun 就使用
    # 新端点和手工音色配置，不必再操作一次控件。
    _set_runtime_config(
        "kokoro",
        "base_url",
        (
            st.session_state.get(
                "kokoro_base_url_input",
                config.kokoro.get("base_url") or DEFAULT_KOKORO_BASE_URL,
            )
            or ""
        ).strip(),
    )
    _set_runtime_config(
        "kokoro",
        "api_key",
        st.session_state.get(
            "kokoro_api_key_input", config.kokoro.get("api_key", "")
        ),
    )
    _set_runtime_config(
        "kokoro",
        "model_id",
        (
            st.session_state.get(
                "kokoro_model_input",
                config.kokoro.get("model_id") or DEFAULT_KOKORO_MODEL,
            )
            or DEFAULT_KOKORO_MODEL
        ).strip(),
    )
    _set_runtime_config(
        "kokoro",
        "voices",
        _parse_chatterbox_voices(
            st.session_state.get(
                "kokoro_voices_input",
                config.kokoro.get("voices") or DEFAULT_KOKORO_VOICES,
            )
        ),
    )


def _get_kokoro_voice_options(saved_voice_name: str) -> list[str]:
    """会话内短缓存远端目录，断线时保留上次选择，不把故障当成用户改音色。"""
    if config.kokoro.get("voices"):
        return voice.get_kokoro_voices()

    # 仅保留当前服务的一条缓存。更换端点/凭据立即重查，缓存不保存明文 Key；
    # 30 秒内的其他 UI 操作不重复阻塞 5 秒等待一个已知离线的服务。
    signature = (
        (config.kokoro.get("base_url") or "").strip().rstrip("/"),
        _credential_signature(config.kokoro.get("api_key", "")),
    )
    catalog = st.session_state.get("kokoro_voice_catalog", {})
    if catalog.get("signature") != signature:
        catalog = {"signature": signature, "voices": [], "checked_at": None}
    now = time.monotonic()
    if catalog["checked_at"] is None or now - catalog["checked_at"] >= 30:
        fetched = voice.get_kokoro_voices(fallback=False)
        catalog.update(checked_at=now, available=bool(fetched))
        if fetched:
            catalog["voices"] = fetched
        st.session_state["kokoro_voice_catalog"] = catalog

    options = list(catalog["voices"])
    if not catalog["available"]:
        st.warning(tr("Kokoro Voices Unavailable"))
        # 首次打开时可能没有缓存，仍保留配置文件中的真实选择；恢复连接后
        # 只有成功返回的新目录才能判定某个旧音色确实已被服务器删除。
        if voice.is_kokoro_voice(saved_voice_name) and saved_voice_name not in options:
            options.insert(0, saved_voice_name)
    return options or [f"kokoro:{voice.KOKORO_DEFAULT_VOICE}"]


def _detect_audio_mime(audio_file: str, audio_bytes: bytes) -> str:
    # 有些 OpenAI-compatible TTS 服务，例如 travisvn/chatterbox-tts-api，
    # 即使请求 response_format=mp3，也会返回 WAV 内容。WebUI 试听如果固定
    # 使用 audio/mp3，浏览器可能无法播放，因此这里按文件头识别真实格式。
    header = audio_bytes[:12]
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"ID3") or header[:2] in (
        b"\xff\xfb",
        b"\xff\xf3",
        b"\xff\xf2",
    ):
        return "audio/mp3"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    ext = os.path.splitext(audio_file)[1].lower()
    return {
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }.get(ext, "audio/mp3")


def _build_uploaded_file_path(uploaded_file, target_dir, allowed_extensions, prefix):
    """为浏览器上传文件生成受控的服务端保存路径。"""
    original_name = os.path.basename(str(uploaded_file.name or ""))
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in allowed_extensions:
        logger.warning(
            f"reject unsupported uploaded file extension: {original_name or '<empty>'}"
        )
        raise ValueError("unsupported uploaded file type")

    normalized_target_dir = os.path.realpath(target_dir)
    os.makedirs(normalized_target_dir, exist_ok=True)
    # 不复用浏览器传入的文件名，避免路径分隔符、控制字符或同名覆盖。UUID 只用于
    # 服务端落盘，不改变用户在上传控件中看到的原始名称。
    file_path = os.path.realpath(
        os.path.join(normalized_target_dir, f"{prefix}-{uuid4().hex}{extension}")
    )
    if os.path.commonpath([normalized_target_dir, file_path]) != normalized_target_dir:
        logger.warning(f"invalid uploaded file path: {file_path}")
        raise ValueError("invalid uploaded file path")
    return file_path


def _save_uploaded_local_materials(uploaded_files):
    """Validate a WebUI material batch and undo earlier files on failure."""
    local_videos_dir = utils.storage_dir("local_videos", create=True)
    materials = []
    persisted = []
    saved_paths = []
    try:
        for uploaded_file in uploaded_files:
            stored_name = material_upload_service.save_material_upload(
                uploaded_file.name, uploaded_file
            )
            file_path = os.path.join(local_videos_dir, stored_name)
            saved_paths.append(file_path)
            material_info = MaterialInfo()
            material_info.provider = "local"
            material_info.url = file_path
            materials.append(material_info)
            persisted.append(
                {
                    "provider": material_info.provider,
                    "url": material_info.url,
                    "duration": material_info.duration,
                }
            )
    except Exception:
        for file_path in saved_paths:
            try:
                os.remove(file_path)
            except OSError as exc:
                logger.warning(
                    f"failed to remove local material after batch error: "
                    f"path={file_path}, error={exc}"
                )
        raise
    return materials, persisted


def _initialize_session_state():
    """集中初始化跨 rerun 保留的页面状态。"""
    if not st.session_state.get("cross_post_recovery_checked"):
        # WebUI 可以不经过 FastAPI 独立运行，因此也需要在首次会话初始化时处理
        # 进程重启留下的发布状态。恢复失败时不写标记，后续 rerun 会再次尝试。
        recovered = tm.recover_interrupted_cross_posts()
        if recovered is not None:
            st.session_state["cross_post_recovery_checked"] = True

    saved_ui_language = config.ui.get("language", "")
    browser_locale = st.context.locale
    initial_ui_language = utils.resolve_ui_language(
        saved_language=saved_ui_language,
        browser_locale=browser_locale,
        supported_languages=locales.keys(),
    )

    defaults = {
        "video_subject": "",
        "video_script": "",
        "video_terms": "",
        "stock_video_style_select": "default",
        "visual_style_select": VISUAL_STYLE_LABELS["savage_animated_explainer"],
        "paragraph_number_input": _saved_ui_number(
            "paragraph_number",
            1,
            llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
            llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
            int,
        ),
        "video_script_prompt": _saved_ui_text(
            "video_script_prompt",
            max_length=llm.MAX_SCRIPT_PROMPT_LENGTH,
        ),
        "custom_system_prompt": _saved_ui_text(
            "custom_system_prompt",
            llm.DEFAULT_SCRIPT_SYSTEM_PROMPT,
            llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
        ),
        "match_materials_to_script": bool(
            config.app.get("match_materials_to_script", False)
        ),
        "custom_bgm_file_input": _saved_ui_text("custom_bgm_file"),
        "sonilo_bgm_prompt_input": _saved_ui_text(
            "sonilo_bgm_prompt",
            max_length=sonilo_service.MAX_PROMPT_LENGTH,
        ),
        "elevenlabs_music_prompt_input": _saved_ui_text(
            "elevenlabs_music_prompt",
            max_length=elevenlabs_music_service.MAX_PROMPT_LENGTH,
        ),
        "subtitle_enabled_checkbox": _saved_ui_bool("subtitle_enabled", True),
        "stroke_color_picker": _saved_ui_color("stroke_color", "#000000"),
        "stroke_width_slider": _saved_ui_number(
            "stroke_width", 1.5, 0.0, 10.0
        ),
        "loomloom_candidate_count": _saved_ui_number(
            "loomloom_candidate_count",
            3,
            1,
            loomloom.MAX_SCRIPT_CANDIDATES,
            int,
        ),
        "loomloom_script_duration_seconds": _saved_ui_number(
            "loomloom_script_duration_seconds", 60, 10, 600, int
        ),
        "ui_language": initial_ui_language,
        # 已落盘的本地素材允许用户只修改文案后继续复用。
        "local_video_materials": [],
        # 生成按钮回调先登记任务，使顶部入口能立即显示运行中数量。
        "active_generation_tasks": {},
        # 最近一次从当前页面提交的任务。生成改为后台执行后，页面 Fragment
        # 通过这个 ID 查询状态；刷新时不再依赖正在执行的旧页面脚本。
        "current_generation_task_id": "",
        # LoomLoom 询价与执行必须跨 Streamlit rerun 保留完全相同的输入和
        # clientRequestId，避免网络重试产生重复付费任务。
        "loomloom_script_batch": None,
        "loomloom_script_quote": None,
        "loomloom_script_input_signature": "",
        "loomloom_client_request_id": "",
        "loomloom_run_id": "",
        "loomloom_run_status": "",
        "loomloom_run_error": "",
        "loomloom_poll_failure_count": 0,
        "loomloom_poll_retry_after": 0.0,
        "loomloom_poll_paused": False,
        "loomloom_script_candidates": (),
        "loomloom_candidate_errors": (),
        "loomloom_selected_candidate": 0,
        "loomloom_video_batch": None,
        "loomloom_video_quote": None,
        "loomloom_video_input_signature": "",
        "loomloom_video_client_request_id": "",
        "loomloom_video_confirm_charge": False,
        "loomloom_video_quote_error_signature": "",
        "loomloom_video_quote_error": "",
        "loomloom_video_capability": None,
        "loomloom_video_capability_fingerprint": "",
        "loomloom_video_capability_load_attempt": "",
        "loomloom_video_capability_error": "",
        "loomloom_video_model_id": "",
        # 文案或完整配音刚生成时，在视频数量控件创建前消费这个摘要并自动
        # 填入推荐素材数；消费后即清空，避免覆盖用户后续手动调整。
        "loomloom_video_scene_autofill_digest": "",
        "wavespeed_confirm_charge": False,
        "volcengine_seedance_confirm_charge": False,
        "ofox_confirm_charge": False,
        "metaso_minimax_confirm_charge": False,
        "muapi_confirm_charge": False,
        # AI 视频按素材段计费，默认只生成一段，用户确认效果后再主动增加数量。
        "loomloom_video_scene_count": _saved_ui_number(
            "loomloom_video_scene_count",
            1,
            1,
            loomloom.MAX_VIDEO_SCENES,
            int,
        ),
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    # Upgrade sessions created before Savage became the production default.
    # Explicit choices and task-restored values remain untouched.
    migrate_create_video_style(
        st.session_state, VISUAL_STYLE_LABELS, VISUAL_STYLE_VALUES
    )


_initialize_session_state()


def tr(key):
    loc = locales.get(st.session_state["ui_language"], {})
    value = loc.get("Translation", {}).get(key)
    if value is not None:
        return value
    # 新功能优先维护中英文。其它语言缺少单项翻译时统一回退英文，避免在多个
    # locale 中复制相同英文后长期失去同步；英文也没有该键时才显示原始 key。
    return locales.get("en", {}).get("Translation", {}).get(key, key)


# -----------------------------------------------------------------------------
# 任务管理：历史扫描、运行状态、参数恢复与列表交互
# -----------------------------------------------------------------------------


def _format_task_time(timestamp):
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _format_task_subject(subject, max_length=30):
    subject = str(subject or "").replace("\n", " ").strip()
    if len(subject) <= max_length:
        return subject or "-"
    return f"{subject[:max_length]}..."


def _safe_load_task_script(task_path):
    script_file = os.path.join(task_path, "script.json")
    if not os.path.isfile(script_file):
        return {}

    try:
        with open(script_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            logger.warning(f"task script data is not an object: {script_file}")
            return {}
        return payload
    except Exception as e:
        logger.warning(f"failed to read task script data: {script_file}, {e}")
        return {}


def _find_final_task_video(task_path: str) -> str:
    """
    返回任务目录中序号最小的最终成片。

    合成流程还会产生 combined、temp-clip 和 MoviePy 临时文件，这些文件不能
    表示任务已成功完成，因此这里只接受 ``final-<序号>.<扩展名>``。
    """
    try:
        files = os.listdir(task_path)
    except OSError:
        return ""

    candidates = []
    for file_name in files:
        match = _FINAL_VIDEO_PATTERN.fullmatch(file_name)
        if match and os.path.isfile(os.path.join(task_path, file_name)):
            candidates.append((int(match.group("index")), file_name))

    if not candidates:
        return ""

    _, file_name = min(candidates, key=lambda item: item[0])
    return os.path.join(task_path, file_name)


def _build_restore_upload_requirements(params: Mapping) -> dict:
    """
    记录历史任务中无法由 Streamlit 自动恢复的上传文件依赖。

    浏览器不允许程序重新填充 file_uploader，因此恢复任务时需要单独记录本地
    素材和自定义音频依赖，并在用户重新生成前检查是否已经主动补充或替换。
    """
    return {
        "local_materials": params.get("video_source") == "local",
        "custom_audio": bool(params.get("custom_audio_file")),
        "original_voice_name": params.get("voice_name") or "",
    }


def _get_unmet_restore_upload_requirements(
    requirements: Mapping | None,
    *,
    video_source: str,
    voice_name: str,
    has_local_materials: bool,
    has_custom_audio: bool,
    voice_mode: str | None = None,
) -> set[str]:
    """返回当前表单仍未满足的历史上传文件依赖。"""
    requirements = requirements or {}
    unmet = set()

    if (
        requirements.get("local_materials")
        and video_source == "local"
        and not has_local_materials
    ):
        unmet.add("local_materials")

    if requirements.get("custom_audio") and not has_custom_audio:
        if voice_mode is not None:
            # 新版 WebUI 使用显式配音方式。用户切换到自动配音或无配音，表示
            # 已主动替换历史上传音频；只有继续选择上传模式时才要求重新上传。
            if voice_mode == VOICE_MODE_UPLOAD:
                unmet.add("custom_audio")
        elif voice_name == requirements.get("original_voice_name", ""):
            # 保留旧调用方按音色判断的兼容行为，避免影响 API 和已有测试工具。
            unmet.add("custom_audio")

    return unmet


def _queue_task_restore(task_id):
    # 任务列表运行在 fragment 中，不能直接修改已经创建的主表单控件状态。
    # 这里只记录候选任务并触发整页 rerun，确认和参数恢复由主页面统一处理。
    st.session_state["task_restore_candidate_id"] = task_id
    st.session_state["task_manager_popover_nonce"] = (
        st.session_state.get("task_manager_popover_nonce", 0) + 1
    )
    st.rerun(scope="app")


def _normalize_task_state(state):
    if state in (
        const.TASK_STATE_COMPLETE,
        const.TASK_STATE_FAILED,
        const.TASK_STATE_PROCESSING,
    ):
        return state
    try:
        return int(state)
    except (TypeError, ValueError):
        return state


def _active_generation_tasks():
    tasks = st.session_state.setdefault("active_generation_tasks", {})
    if not isinstance(tasks, dict):
        tasks = {}
        st.session_state["active_generation_tasks"] = tasks
    return tasks


def _add_active_generation_task(task_id, subject=None):
    tasks = _active_generation_tasks()
    task = tasks.setdefault(task_id, {})
    task["subject"] = subject or task.get("subject") or task_id
    task["mtime"] = task.get("mtime") or datetime.now().timestamp()


def _remove_active_generation_task(task_id):
    tasks = _active_generation_tasks()
    if task_id in tasks:
        del tasks[task_id]
    if st.session_state.get("pending_generation_task_id") == task_id:
        del st.session_state["pending_generation_task_id"]


def _prepare_generation_task():
    # st.button 的 on_click 会在页面脚本重新执行前触发。这里提前生成任务 ID，
    # 顶部任务管理入口就能在同一次 rerun 中显示“生成中”数量。
    task_id = str(uuid4())
    st.session_state["pending_generation_task_id"] = task_id
    subject = st.session_state.get("video_subject") or st.session_state.get(
        "video_script"
    )
    _add_active_generation_task(task_id, subject=subject)


def _task_state_label(state, has_video):
    normalized_state = _normalize_task_state(state)
    if normalized_state == const.TASK_STATE_COMPLETE:
        return tr("Task Status Complete")
    if normalized_state == const.TASK_STATE_FAILED:
        return tr("Task Status Failed")
    if normalized_state == const.TASK_STATE_PROCESSING:
        return tr("Task Status Processing")
    if has_video:
        return tr("Task Status Complete")
    return tr("Task Status History")


def _task_state_filter_key(task):
    normalized_state = _normalize_task_state(task.get("state"))
    if normalized_state == const.TASK_STATE_PROCESSING:
        return "processing"
    if normalized_state == const.TASK_STATE_FAILED:
        return "failed"
    if normalized_state == const.TASK_STATE_COMPLETE or task["video_file"]:
        return "complete"
    return "history"


def _scan_history_tasks(limit=30):
    tasks_root = utils.task_dir()
    if not os.path.isdir(tasks_root):
        return []

    # 任务管理 fragment 每两秒刷新一次。先只读取低成本的目录元数据并截取最近
    # 的任务，再解析 script.json 和视频列表，避免历史任务很多时反复扫描全部内容。
    task_entries = []
    try:
        with os.scandir(tasks_root) as entries:
            for entry in entries:
                try:
                    if entry.name.startswith(".") or not entry.is_dir(
                        follow_symlinks=False
                    ):
                        continue
                    task_entries.append(
                        (
                            entry.stat(follow_symlinks=False).st_mtime,
                            entry.name,
                            entry.path,
                        )
                    )
                except OSError as e:
                    # 单个任务目录可能正在被删除，不应因此让整个任务面板失效。
                    logger.debug(f"skip unavailable task directory: {entry.path}, {e}")
    except OSError as e:
        logger.warning(f"failed to scan task directory: {tasks_root}, {e}")
        return []

    task_entries.sort(key=lambda item: item[0], reverse=True)
    tasks = []
    for mtime, name, task_path in task_entries[:limit]:
        script_data = _safe_load_task_script(task_path)
        params_data = script_data.get("params", {})
        if not isinstance(params_data, dict):
            params_data = {}
        script_text = script_data.get("script", "")
        if not isinstance(script_text, str):
            script_text = ""
        video_file = _find_final_task_video(task_path)
        subject = (
            params_data.get("video_subject")
            or script_text[:40]
            or name
        )
        tasks.append(
            {
                "task_id": name,
                "subject": subject,
                "state": const.TASK_STATE_COMPLETE if video_file else None,
                "progress": 100 if video_file else 0,
                "mtime": mtime,
                "task_path": task_path,
                "video_file": video_file,
                "source": "history",
            }
        )

    return tasks


def _collect_task_summaries(limit=20):
    history_tasks = {task["task_id"]: task for task in _scan_history_tasks(limit=50)}
    active_tasks = _active_generation_tasks()

    try:
        runtime_tasks, _ = sm.state.get_all_tasks(1, 50)
    except Exception as e:
        logger.warning(f"failed to load runtime tasks: {e}")
        runtime_tasks = []

    # The paginated state view can omit this session's newer tasks after 50
    # older records. Read those active IDs directly so a completed or failed
    # task cannot remain labelled as processing forever.
    runtime_ids = {task.get("task_id") for task in runtime_tasks}
    for task_id in active_tasks:
        if task_id in runtime_ids:
            continue
        try:
            task = sm.state.get_task(task_id)
        except Exception as e:
            logger.warning(f"failed to load active task {task_id}: {e}")
            continue
        if task:
            runtime_tasks.append(task)

    for task in runtime_tasks:
        task_id = task.get("task_id", "")
        if not task_id:
            continue

        task_path = os.path.join(utils.task_dir(), task_id)
        history_task = history_tasks.get(task_id, {})
        video_files = task.get("videos") or []
        video_file = (
            video_files[0] if video_files else history_task.get("video_file", "")
        )
        subject = (
            task.get("video_subject")
            or history_task.get("subject")
            or (task.get("script", "")[:40] if task.get("script") else "")
            or task_id
        )
        task_mtime = active_tasks.get(task_id, {}).get("mtime") or history_task.get(
            "mtime", 0
        )
        if os.path.isdir(task_path):
            try:
                task_mtime = os.path.getmtime(task_path)
            except OSError:
                # Another session can delete this directory between isdir and
                # getmtime. Keep rendering the persisted task state.
                pass

        history_tasks[task_id] = {
            "task_id": task_id,
            "subject": subject,
            "state": task.get("state"),
            "cross_post_state": task.get("cross_post_state"),
            "progress": int(task.get("progress", 0) or 0),
            "mtime": task_mtime,
            "task_path": task_path,
            "video_file": video_file,
            "source": "runtime",
        }

    for task_id, active_task in active_tasks.items():
        history_task = history_tasks.get(task_id, {})
        if history_task and _task_state_filter_key(history_task) in {
            "complete",
            "failed",
        }:
            # 会话中的 active 标记只负责覆盖任务刚提交到状态存储前的极短窗口。
            # 后台任务结束后必须以真实终态为准，不能把失败任务重新显示为生成中。
            continue

        task_path = os.path.join(utils.task_dir(), task_id)
        history_tasks[task_id] = {
            "task_id": task_id,
            "subject": active_task.get("subject")
            or history_task.get("subject")
            or task_id,
            "state": const.TASK_STATE_PROCESSING,
            "progress": history_task.get("progress", 0),
            "mtime": active_task.get("mtime")
            or history_task.get("mtime", datetime.now().timestamp()),
            "task_path": task_path,
            "video_file": history_task.get("video_file", ""),
            "source": "active",
        }

    tasks = list(history_tasks.values())
    return sorted(tasks, key=lambda item: item["mtime"], reverse=True)[:limit]


def _is_headless_server():
    # Docker 或无桌面的服务器部署中，WebUI 进程接触不到用户的桌面环境：
    # xdg-open / webbrowser 只会在容器内静默失败。此时应改为浏览器内预览
    # 视频、以路径提示代替打开目录。macOS/Windows 桌面部署不受影响。
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _open_task_path(task_path):
    tasks_root = os.path.abspath(utils.task_dir())
    normalized_path = os.path.abspath(task_path)
    if not normalized_path.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task folder path: {normalized_path}")
        return
    if not os.path.isdir(normalized_path):
        return
    if _is_headless_server():
        # storage 目录通常以卷挂载映射回宿主机，提示相对路径即可定位文件。
        rel_path = os.path.relpath(normalized_path, os.path.dirname(tasks_root))
        st.toast(f"{tr('Open Task Folder')}: ./storage/{rel_path}", icon="📂")
        return
    webbrowser.open(f"file://{normalized_path}")


def _open_task_video(video_file):
    tasks_root = os.path.abspath(utils.task_dir())
    normalized_file = os.path.abspath(video_file)

    # 视频路径来自任务目录扫描或运行期状态。这里仍然限制只能打开任务目录
    # 内的文件，避免 UI 操作被异常路径扩展成任意本地文件打开能力。
    if not normalized_file.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task video path: {normalized_file}")
        return
    if not os.path.isfile(normalized_file):
        logger.warning(f"task video does not exist: {normalized_file}")
        return

    if _is_headless_server():
        # 无桌面环境时在任务面板内嵌播放器预览，代替调用系统播放器。
        st.session_state["task_preview_video_file"] = normalized_file
        return

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", normalized_file])
        elif sys.platform.startswith("win"):
            os.startfile(normalized_file)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", normalized_file])
    except Exception as e:
        logger.error(f"failed to open task video: {normalized_file}, {e}")


def _delete_task(task_id, task_path, task_state=None):
    # 页面展示的状态可能落后于后台任务。删除前同时检查传入状态、当前会话的
    # 活跃任务和最新状态，避免任务刚开始或已产出中间视频时被误删。
    current_task = None
    try:
        current_task = sm.state.get_task(task_id)
    except Exception as e:
        logger.exception(f"failed to verify task state before deletion: {task_id}, {e}")
        return False

    task_snapshot = dict(current_task or {})
    task_snapshot.setdefault("state", task_state)
    if task_id in _active_generation_tasks():
        task_snapshot["state"] = const.TASK_STATE_PROCESSING

    if tm.is_task_busy(task_snapshot):
        logger.warning(f"refused to delete running task: {task_id}")
        return False

    tasks_root = os.path.abspath(utils.task_dir())
    normalized_path = os.path.abspath(task_path)

    # 删除任务会移除任务状态和本地生成文件。这里必须限定在 storage/tasks
    # 下，避免异常 task_path 造成误删其它本地目录。
    if not normalized_path.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task folder path for deletion: {normalized_path}")
        return False

    try:
        if os.path.isdir(normalized_path):
            shutil.rmtree(normalized_path)
        # Keep the persisted task available for retry when file removal fails
        # (for example, a video is still locked by another process on Windows).
        if hasattr(sm.state, "delete_task"):
            sm.state.delete_task(task_id)
        logger.info(f"deleted task: {task_id}")
        return True
    except Exception as e:
        logger.exception(f"failed to delete task: {task_id}, {e}")
        return False


def _count_processing_tasks(tasks):
    # 顶部任务管理入口只需要展示“生成中”任务数量。
    # 这里复用内部状态 key 判断，避免依赖多语言展示文案导致不同语言下统计不一致。
    processing_task_ids = {
        task["task_id"]
        for task in tasks
        if _task_state_filter_key(task) == "processing"
    }
    return len(processing_task_ids)


def _task_manager_label(processing_count):
    label = tr("Task Manager")
    if processing_count <= 0:
        return label
    return f"{label} · {processing_count}"


def _build_video_download_name(subject, index, total):
    """根据视频主题生成跨平台安全的下载文件名。"""
    safe_subject = _DOWNLOAD_FILENAME_INVALID_PATTERN.sub(" ", str(subject or ""))
    safe_subject = re.sub(r"\s+", " ", safe_subject).strip(" .")[:80].rstrip(" .")
    if not safe_subject:
        safe_subject = "video"
    # Win32 在识别设备名时会忽略扩展名前的尾随空格和句点。与背景音乐上传
    # 的现有规则保持一致，避免 ``CON .topic`` 绕过保留名保护。
    windows_basename = safe_subject.split(".", 1)[0].rstrip(" .").upper()
    if windows_basename in _WINDOWS_RESERVED_FILENAMES:
        safe_subject = f"_{safe_subject}"

    suffix = f"-{index}" if total > 1 else ""
    return f"{safe_subject}{suffix}.mp4"


def _render_task_table(filtered_tasks, key_prefix):
    with st.container(key=f"task_table_header_{key_prefix}"):
        header_cols = st.columns([1.1, 1.7, 3.0, 0.8, 1.6], vertical_alignment="center")
        header_cols[0].caption(tr("Task Status"))
        header_cols[1].caption(tr("Task Updated At"))
        header_cols[2].caption(tr("Task Subject"))
        header_cols[3].caption(tr("Task Progress"))
        header_cols[4].caption(tr("Task Actions"))

    if not filtered_tasks:
        st.info(tr("No Tasks Match Filter"))
        return

    visible_tasks = filtered_tasks[:12]
    list_height = min(390, max(96, len(visible_tasks) * 58))
    with st.container(height=list_height, border=False):
        for task in visible_tasks:
            task_id = task["task_id"]
            has_video = bool(task["video_file"] and os.path.isfile(task["video_file"]))
            is_processing = _task_state_filter_key(task) == "processing"
            is_busy = is_processing or tm.is_task_busy(task)
            has_restore_data = os.path.isfile(
                os.path.join(task["task_path"], "script.json")
            )
            safe_task_key = "".join(ch if ch.isalnum() else "_" for ch in task_id)[:40]

            # 使用 Streamlit 原生 bordered container + columns 保留每行操作。
            # 相比自定义 HTML/CSS 表格，这种方式对 Streamlit 版本变更更稳；
            # 相比 dataframe，又能保留播放、打开目录、删除等行内动作。
            with st.container(
                key=f"task_row_{key_prefix}_{safe_task_key}", border=True
            ):
                row_cols = st.columns(
                    [1.1, 1.7, 3.0, 0.8, 1.6],
                    vertical_alignment="center",
                )
                row_cols[0].write(_task_state_label(task["state"], has_video))
                row_cols[1].write(_format_task_time(task["mtime"]))
                row_cols[2].write(_format_task_subject(task["subject"]))
                row_cols[3].write(f"{task['progress']}%")

                action_cols = row_cols[4].columns(
                    4,
                    vertical_alignment="center",
                    gap="small",
                )
                with action_cols[0]:
                    play_label = tr("Play")
                    if st.button(
                        play_label,
                        key=f"play_task_{key_prefix}_{task_id}",
                        width="stretch",
                        icon=":material/play_arrow:",
                        help=play_label,
                        disabled=not has_video,
                    ):
                        _open_task_video(task["video_file"])

                with action_cols[1]:
                    open_label = tr("Open Task Folder")
                    if st.button(
                        open_label,
                        key=f"open_task_{key_prefix}_{task_id}",
                        width="stretch",
                        icon=":material/folder_open:",
                        help=open_label,
                    ):
                        _open_task_path(task["task_path"])

                with action_cols[2]:
                    restore_label = tr("Regenerate Task")
                    if st.button(
                        restore_label,
                        key=f"restore_task_{key_prefix}_{task_id}",
                        width="stretch",
                        icon=":material/replay:",
                        help=restore_label,
                        disabled=is_processing or not has_restore_data,
                    ):
                        _queue_task_restore(task_id)

                with action_cols[3]:
                    delete_label = tr("Delete Task")
                    delete_help = (
                        f"{delete_label} ({tr('Task Status Processing')})"
                        if is_busy
                        else delete_label
                    )
                    if st.button(
                        delete_label,
                        key=f"delete_task_{key_prefix}_{task_id}",
                        width="stretch",
                        icon=":material/delete:",
                        help=delete_help,
                        disabled=is_busy,
                    ):
                        if _delete_task(task_id, task["task_path"], task["state"]):
                            st.toast(tr("Task Deleted"))
                            st.rerun()
                        else:
                            st.error(tr("Task Delete Failed"))


def _render_task_manager_panel(tasks=None):
    tasks = tasks if tasks is not None else _collect_task_summaries()
    if not tasks:
        st.info(tr("No Tasks Yet"))
        return

    # Streamlit 1.59 支持有状态 Tabs 的惰性渲染。切换时只重新构建当前列表，
    # 避免定时 Fragment 每两秒重复创建四套任务行和操作按钮。
    status_tabs = [
        ("all", tr("All Tasks")),
        ("processing", tr("Task Status Processing")),
        ("complete", tr("Task Status Complete")),
        ("failed", tr("Task Status Failed")),
    ]
    tabs = st.tabs(
        [label for _, label in status_tabs],
        key="task_manager_status_tabs",
        on_change="rerun",
    )
    for (status_key, _), tab in zip(status_tabs, tabs):
        if not tab.open:
            continue
        with tab:
            filtered_tasks = [
                task
                for task in tasks
                if status_key == "all" or _task_state_filter_key(task) == status_key
            ]
            _render_task_table(filtered_tasks, status_key)

    _render_task_video_preview()


def _render_task_video_preview():
    # 无桌面部署下“播放”按钮的浏览器内回退：在任务面板底部渲染播放器。
    preview_file = st.session_state.get("task_preview_video_file")
    if not preview_file:
        return

    tasks_root = os.path.abspath(utils.task_dir())
    if not (
        preview_file.startswith(tasks_root + os.sep) and os.path.isfile(preview_file)
    ):
        st.session_state.pop("task_preview_video_file", None)
        return

    st.divider()
    preview_cols = st.columns([5, 1], vertical_alignment="center")
    task_name = os.path.basename(os.path.dirname(preview_file))
    preview_cols[0].caption(f"{os.path.basename(preview_file)} · {task_name}")
    closed = preview_cols[1].button(
        "✕",
        key="close_task_video_preview",
        width="stretch",
        help=tr("Close"),
    )
    if closed:
        st.session_state.pop("task_preview_video_file", None)
        return
    st.video(preview_file)


@st.fragment(run_every="2s")
def _render_task_manager_entry():
    # 任务可能由当前页面或其它页面触发生成。入口单独用 fragment 定时刷新，
    # 只更新任务数量和 popover 内容，不打断主页面表单输入。
    task_summaries = _collect_task_summaries()
    processing_task_count = _count_processing_tasks(task_summaries)
    with st.container(key="task_manager_entry", width="content"):
        with st.popover(
            _task_manager_label(processing_task_count),
            width="content",
            key=(
                "task_manager_popover_"
                f"{st.session_state.get('task_manager_popover_nonce', 0)}"
            ),
        ):
            _render_task_manager_panel(task_summaries)


def _load_task_restore_payload(task_id):
    tasks_root = os.path.realpath(utils.task_dir())
    task_path = os.path.realpath(os.path.join(tasks_root, str(task_id)))
    try:
        if os.path.commonpath([tasks_root, task_path]) != tasks_root:
            raise ValueError("task path is outside the task directory")
    except ValueError as e:
        logger.warning(f"invalid task restore path: {task_id}, {e}")
        return None

    script_data = _safe_load_task_script(task_path)
    raw_params = script_data.get("params")
    if not isinstance(raw_params, dict):
        logger.warning(f"task has no restorable parameters: {task_id}")
        return None

    params_input = dict(raw_params)
    if script_data.get("script"):
        params_input["video_script"] = script_data["script"]
    if script_data.get("search_terms"):
        params_input["video_terms"] = script_data["search_terms"]

    try:
        params = VideoParams.model_validate(params_input).model_dump(mode="json")
    except Exception as e:
        logger.warning(f"failed to validate task restore parameters: {task_id}, {e}")
        return None

    return {
        "task_id": str(task_id),
        "subject": params.get("video_subject") or script_data.get("script") or task_id,
        "params": params,
    }


def _infer_tts_server_from_voice(voice_name):
    if voice.is_no_voice(voice_name):
        return voice.NO_VOICE_NAME
    if voice.is_siliconflow_voice(voice_name):
        return "siliconflow"
    if voice.is_gemini_voice(voice_name):
        return "gemini-tts"
    if voice.is_mimo_voice(voice_name):
        return "mimo-tts"
    if voice.is_minimax_voice(voice_name):
        return "minimax-tts"
    if voice.is_elevenlabs_voice(voice_name):
        return "elevenlabs"
    if voice.is_chatterbox_voice(voice_name):
        return "chatterbox"
    if voice.is_kokoro_voice(voice_name):
        return "kokoro"
    if voice.is_fish_audio_voice(voice_name):
        return "fish_audio"
    if voice.is_voxcpm_voice(voice_name):
        return "voxcpm"
    if voice.is_azure_v2_voice(voice_name):
        return "azure-tts-v2"
    return "azure-tts-v1"


def _set_stable_widget_value(key, value):
    if value is not None:
        st.session_state[localized_widget_key(key)] = value


def _apply_pending_task_restore():
    payload = st.session_state.pop("task_restore_payload", None)
    if not payload:
        return False

    _apply_restored_params(payload["params"])
    st.session_state["task_restore_succeeded"] = True
    logger.info(f"restored task configuration: {payload['task_id']}")
    return True


def _apply_restored_params(params):
    """
    把一份完整的生成参数写回页面控件状态。

    历史任务恢复和设置预设导入使用同一份参数模型，因此共用同一个实现，避免
    新增字段时只更新其中一条路径。调用方必须在渲染任何控件之前执行，否则
    Streamlit 会拒绝修改已经实例化的控件状态。
    """
    video_terms = params.get("video_terms") or ""
    if isinstance(video_terms, list):
        video_terms = ", ".join(str(term) for term in video_terms)

    # 文案与高级脚本设置。
    st.session_state["video_subject"] = params.get("video_subject") or ""
    st.session_state["video_script"] = params.get("video_script") or ""
    st.session_state["video_terms"] = str(video_terms)
    _set_stable_widget_value(
        "script_language_select", params.get("video_language") or ""
    )
    st.session_state["paragraph_number_input"] = params.get("paragraph_number", 1)
    st.session_state["video_script_prompt"] = params.get("video_script_prompt") or ""
    st.session_state["custom_system_prompt"] = (
        params.get("custom_system_prompt") or llm.DEFAULT_SCRIPT_SYSTEM_PROMPT
    )

    # 视频设置。素材上传控件不能由服务端写入，因此本地素材需要用户重新选择。
    video_source = params.get("video_source") or "pexels"
    _set_stable_widget_value("video_source_select", video_source)
    _set_stable_widget_value(
        "stock_video_style_select", params.get("stock_video_style") or "default"
    )
    restored_stock_policy = (
        params.get("stock_media_policy")
        or config.app.get("stock_media_policy", "auto_eligible")
    )
    if restored_stock_policy not in {"manual_review", "auto_eligible"}:
        restored_stock_policy = "manual_review"
    _set_stable_widget_value("stock_media_policy_select", restored_stock_policy)
    _set_stable_widget_value(
        "visual_style_select",
        VISUAL_STYLE_LABELS.get(
            params.get("visual_style") or (
                "asmr_cutting"
                if params.get("stock_video_style") == "satisfying_asmr_cutting"
                else "savage_animated_explainer"
            ),
            VISUAL_STYLE_LABELS["auto"],
        ),
    )
    restored_style = params.get("visual_style") or "savage_animated_explainer"
    st.session_state["_visual_style_user_selected"] = True
    st.session_state["_active_visual_style_preset"] = restored_style
    for field, key in (
        ("narrator_preset", "narrator_preset_select"),
        ("profanity_level", "profanity_level_select"),
        ("subtitle_display_mode", "subtitle_display_mode_select"),
        ("subtitle_animation", "subtitle_animation_select"),
    ):
        if params.get(field) is not None:
            _set_stable_widget_value(key, params[field])
    if params.get("text_fore_color") is not None:
        st.session_state["font_color_picker"] = params["text_fore_color"]
    restored_server = params.get("tts_provider") or _infer_tts_server_from_voice(
        params.get("voice_name") or ""
    )
    if restored_server:
        _set_stable_widget_value("tts_server_select", restored_server)
        _set_stable_widget_value(
            f"speech_synthesis_select_{restored_server}", params.get("voice_name") or ""
        )
    if params.get("tts_model"):
        _set_runtime_config("ui", "gemini_tts_model", params["tts_model"])
    st.session_state["video_script_needs_style_review"] = False
    _set_stable_widget_value(
        "video_concat_mode_select", params.get("video_concat_mode") or "random"
    )
    _set_stable_widget_value(
        "video_transition_mode_select",
        params.get("video_transition_mode") or VideoTransitionMode.none.value,
    )
    _set_stable_widget_value(
        f"video_aspect_for_{video_source}",
        params.get("video_aspect") or VideoAspect.portrait.value,
    )
    _set_stable_widget_value(
        "video_fit_mode_select",
        params.get("video_fit_mode") or VideoFitMode.cover.value,
    )
    _set_stable_widget_value(
        "image_motion_select", params.get("image_motion") or ImageMotion.zoom.value
    )
    _set_stable_widget_value(
        "video_clip_duration_select", params.get("video_clip_duration", 3)
    )
    _set_stable_widget_value(
        "video_clip_speed_slider",
        # API 可以写入超过 WebUI 范围的速度，任务生成阶段会安全归一化，但
        # 历史记录仍可能保留原值。恢复任务前再次归一化，避免给 Streamlit
        # slider 注入越界值、NaN 或无穷值导致控件状态异常。
        utils.normalize_clip_speed(params.get("video_clip_speed", 1.0)),
    )
    _set_stable_widget_value("video_count_select", params.get("video_count", 1))
    st.session_state["asmr_categories_select"] = params.get(
        "asmr_categories", ["kinetic_sand", "soap"]
    )
    st.session_state["match_materials_to_script"] = bool(
        params.get("match_materials_to_script", False)
    )

    # 音频设置。TTS server 未写入旧任务，根据历史 voice_name 推断。
    voice_name = params.get("voice_name") or voice.NO_VOICE_NAME
    tts_server = _infer_tts_server_from_voice(voice_name)
    if params.get("custom_audio_file"):
        voice_mode = VOICE_MODE_UPLOAD
    elif voice.is_no_voice(voice_name):
        voice_mode = VOICE_MODE_NONE
    else:
        voice_mode = VOICE_MODE_TTS
    _set_stable_widget_value("voice_mode_control", voice_mode)
    if tts_server != voice.NO_VOICE_NAME:
        _set_stable_widget_value("tts_server_select", tts_server)
        _set_stable_widget_value(f"speech_synthesis_select_{tts_server}", voice_name)
    _set_stable_widget_value("voice_volume_select", params.get("voice_volume", 1.0))
    _set_stable_widget_value("voice_rate_select", params.get("voice_rate", 1.0))
    bgm_type = params.get("bgm_type") or ""
    _set_stable_widget_value("bgm_type_select", bgm_type)
    _set_stable_widget_value("bgm_volume_select", params.get("bgm_volume", 0.2))
    if bgm_type == "preset" and params.get("bgm_file"):
        # 预设歌曲控件使用文件名作为稳定业务值。历史任务可能保存绝对路径或
        # 相对路径，统一取 basename 后即可匹配当前安全枚举出的歌曲列表。
        _set_stable_widget_value(
            "preset_song_select", os.path.basename(str(params["bgm_file"]))
        )
    st.session_state["custom_bgm_file_input"] = params.get("bgm_file") or ""
    st.session_state["sonilo_bgm_prompt_input"] = (
        params.get("video_music_prompt") or params.get("sonilo_bgm_prompt") or ""
    )
    st.session_state["elevenlabs_music_prompt_input"] = (
        params.get("video_music_prompt") or ""
    )

    # 字幕设置。对旧任务中的越界数值做最小限幅，避免 Slider 无法初始化。
    st.session_state["subtitle_enabled_checkbox"] = bool(
        params.get("subtitle_enabled", True)
    )
    _set_stable_widget_value("font_name_select", params.get("font_name") or "")
    _set_stable_widget_value(
        "subtitle_position_select", params.get("subtitle_position") or "bottom"
    )
    _set_stable_widget_value(
        "subtitle_display_mode_select", params.get("subtitle_display_mode") or "sentence"
    )
    _set_stable_widget_value(
        "subtitle_animation_select", params.get("subtitle_animation") or "none"
    )
    custom_position = min(100.0, max(0.0, float(params.get("custom_position", 70.0))))
    st.session_state["custom_position_input"] = str(custom_position)
    st.session_state["font_color_picker"] = params.get("text_fore_color") or "#FFFFFF"
    st.session_state["font_size_slider"] = min(
        100, max(30, int(params.get("font_size", 60)))
    )
    st.session_state["stroke_color_picker"] = params.get("stroke_color") or "#000000"
    st.session_state["stroke_width_slider"] = min(
        10.0, max(0.0, float(params.get("stroke_width", 1.5)))
    )
    background_color = params.get("text_background_color")
    background_enabled = bool(background_color)
    st.session_state["subtitle_background_enabled_checkbox"] = background_enabled
    if isinstance(background_color, str):
        st.session_state["subtitle_background_color_picker"] = background_color
    st.session_state["rounded_subtitle_background_checkbox"] = bool(
        params.get("rounded_subtitle_background", False) and background_enabled
    )

    st.session_state.pop("local_video_materials_uploader", None)
    # 历史任务只保存素材路径，不能保证这些文件在当前环境仍然存在。
    # 同时清空当前页面已缓存的上传素材，避免恢复后误用另一个任务的文件。
    st.session_state["local_video_materials"] = []
    st.session_state.pop("custom_audio_file_uploader", None)
    st.session_state.pop("voxcpm_reference_audio_uploader", None)
    st.session_state.pop(VOXCPM_REFERENCE_AUDIO_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY, None)
    st.session_state.pop("voxcpm_prompt_audio_uploader", None)
    st.session_state.pop(VOXCPM_PROMPT_AUDIO_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_PROMPT_TEXT_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_HIGH_FIDELITY_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_PROMPT_EXAMPLE_MODE_SESSION_KEY, None)
    st.session_state.pop("custom_bgm_uploader", None)
    st.session_state.pop("custom_bgm_validation", None)
    st.session_state["task_restore_upload_requirements"] = (
        _build_restore_upload_requirements(params)
    )

    return True


def _dismiss_task_restore_dialog():
    st.session_state.pop("task_restore_candidate_id", None)


@st.dialog(
    tr("Regenerate Task"),
    width="small",
    on_dismiss=_dismiss_task_restore_dialog,
)
def _render_task_restore_dialog(task_id):
    payload = _load_task_restore_payload(task_id)
    if payload is None:
        st.error(tr("Task Restore Failed"))
        if st.button(tr("Cancel"), key="cancel_invalid_task_restore"):
            st.session_state.pop("task_restore_candidate_id", None)
            st.rerun(scope="app")
        return

    st.write(tr("Regenerate Task Confirmation"))
    st.caption(_format_task_subject(payload["subject"], max_length=80))
    cancel_col, load_col = st.columns(2)
    if cancel_col.button(
        tr("Cancel"),
        key="cancel_task_restore",
        width="stretch",
    ):
        st.session_state.pop("task_restore_candidate_id", None)
        st.rerun(scope="app")
    if load_col.button(
        tr("Load Task Configuration"),
        key="confirm_task_restore",
        type="primary",
        width="stretch",
    ):
        st.session_state["task_restore_payload"] = payload
        st.session_state.pop("task_restore_candidate_id", None)
        st.rerun(scope="app")


def _dismiss_settings_dialog():
    """关闭设置弹窗，并确保下一次整页 rerun 不会再次自动打开。"""
    st.session_state["settings_dialog_open"] = False


def _open_settings_dialog(target_tab=None):
    """打开设置弹窗，并可直接定位到指定业务标签页。"""
    st.session_state["settings_dialog_open"] = True
    if target_tab:
        # 这里只保存稳定的业务 ID，不保存翻译文本；真正创建 tabs 前再根据
        # 当前界面语言解析 label，避免用户切换语言后旧文案成为非法选项。
        st.session_state["settings_dialog_target_tab"] = target_tab


def _open_material_settings_dialog():
    """供视频来源组件回调使用：直接打开素材服务设置。"""
    _open_settings_dialog("material")


def _render_brand(available_update: str | None = None):
    """渲染项目名称、当前版本和可选的更新入口。"""
    update_link = ""
    if available_update:
        update_label = html.escape(
            tr("Update Available").format(version=available_update)
        )
        # Streamlit 会继续用 Markdown 解析传入的 HTML。这里保持链接为单行，
        # 避免多行字符串的缩进被识别成代码块，导致页面直接显示 HTML 源码。
        update_link = (
            '<a class="mpt-brand__update" '
            f'href="{version_checker.LATEST_RELEASE_PAGE_URL}" '
            'target="_blank" rel="noopener noreferrer" '
            f'aria-label="{update_label}" title="{update_label}">'
            f"{update_label}</a>"
        )
    st.markdown(
        f"""
        <h1 class="mpt-brand">
            <span class="mpt-brand__name">MoneyPrinterTurbo</span>
            <a class="mpt-brand__version"
               href="https://github.com/harry0703/MoneyPrinterTurbo"
               target="_blank"
               rel="noopener noreferrer"
               aria-label="Open MoneyPrinterTurbo on GitHub"
               title="Open project on GitHub">v{html.escape(str(config.project_version))}</a>
            {update_link}
        </h1>
        """,
        unsafe_allow_html=True,
    )


@st.fragment(run_every="1s")
def _render_pending_version_check():
    """检查未完成时只刷新品牌区域，避免阻塞或反复执行整页表单。"""
    snapshot = version_checker.poll_available_update(config.project_version)
    if snapshot.complete:
        # 检查完成后刷新一次整页，让顶部栏改为静态渲染并停止 fragment 轮询。
        # 该刷新发生在后台请求完成之后，不会延迟初始页面的其它内容。
        st.rerun(scope="app")
    _render_brand()


def _render_top_bar():
    """渲染品牌、任务管理、设置和语言切换组成的页面顶部栏。"""
    # 顶部栏分为品牌区和操作区两个独立区域。窄屏下由 Streamlit
    # 将两个区域整体换行，操作区内部再根据剩余宽度自动换行。
    with st.container(key="top_bar"):
        brand_col, actions_col = st.columns(
            [3.5, 2.0],
            vertical_alignment="center",
            gap="small",
        )

    with brand_col:
        update_snapshot = version_checker.poll_available_update(config.project_version)
        if update_snapshot.complete:
            _render_brand(update_snapshot.available_version)
        else:
            _render_pending_version_check()

    with actions_col:
        with st.container(
            key="top_bar_actions",
            horizontal=True,
            horizontal_alignment="right",
            vertical_alignment="center",
            gap="small",
            width="stretch",
        ):
            _render_task_manager_entry()

            st.button(
                tr("Settings"),
                key="open_settings_dialog_button",
                type="secondary",
                icon=":material/settings:",
                width="content",
                on_click=_open_settings_dialog,
            )

            language_codes = list(locales.keys())
            selected_index = 0
            for i, code in enumerate(language_codes):
                if code == st.session_state.get("ui_language", ""):
                    selected_index = i

            selected_language_code = st.selectbox(
                "Language / 语言",
                options=language_codes,
                index=selected_index,
                format_func=lambda code: locales[code].get("Language", code),
                key="top_language_code_selector",
                label_visibility="collapsed",
                width=180,
            )
            if selected_language_code:
                previous_language = st.session_state.get("ui_language", "")
                if selected_language_code != previous_language:
                    logger.info(
                        "UI language changed by user: "
                        f"previous_language={previous_language or '<empty>'}, "
                        f"selected_language={selected_language_code}"
                    )
                    st.session_state["ui_language"] = selected_language_code
                    # 浏览器自动识别只影响当前会话；只有用户主动切换下拉框时才
                    # 写入 config.toml，后续新会话将优先使用该明确选择。
                    _set_runtime_config("ui", "language", selected_language_code)
                    _save_runtime_config()
                    # 切换语言会先重跑顶部栏，正文控件尚未渲染就触发 rerun。
                    # 显式保留本次创作内容，防止 Streamlit 清理旧控件状态后，
                    # 新语言页面把已经输入的主题、文案和关键词重置为空。
                    for content_key in ("video_subject", "video_script", "video_terms"):
                        if content_key in st.session_state:
                            st.session_state[content_key] = st.session_state[content_key]
                    # 切换语言后强制刷新，避免 selectbox 继续展示旧语言文案。
                    st.rerun()


support_locales = [
    "ca-ES",
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "es-ES",
    "fr-FR",
    "it-IT",
    "ru-RU",
    "vi-VN",
    "th-TH",
    "tr-TR",
]


# -----------------------------------------------------------------------------
# 通用 UI 组件、资源缓存与日志
# -----------------------------------------------------------------------------


@st.cache_data(ttl=30, show_spinner=False)
def get_all_fonts():
    # 字体目录很少变化，但 Streamlit 每次控件交互都会 rerun 页面。短周期缓存
    # 可以避免连续重复 os.walk，同时保证新增字体后最多 30 秒即可被发现。
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                # Keep each option tied to its file, including nested fonts.
                relative_path = os.path.relpath(os.path.join(root, file), font_dir)
                fonts.append(relative_path.replace(os.sep, "/"))
    fonts.sort()
    return fonts


@st.cache_data(ttl=30, show_spinner=False)
def get_all_songs():
    # 背景音乐与字体使用相同的短周期策略，不做永久缓存，兼顾 rerun 性能和
    # 用户运行期间手动添加音乐文件的场景。
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        # task_id 应始终是服务端生成的 UUID。这里先做格式校验，避免异常值
        # 通过路径拼接访问任务目录之外的位置，也避免后续打开目录时触发
        # 平台 shell 对特殊字符的解释。
        normalized_task_id = str(UUID(str(task_id)))
        tasks_root = os.path.abspath(utils.task_dir())
        path = os.path.abspath(os.path.join(tasks_root, normalized_task_id))

        # 即使 UUID 校验通过，也再次确认最终路径仍在任务根目录内，避免
        # 未来调用方调整 task_id 来源时引入路径穿越风险。
        if not path.startswith(tasks_root + os.sep):
            logger.warning(f"invalid task folder path: {path}")
            return

        if os.path.isdir(path):
            webbrowser.open(f"file://{path}")
    except Exception as e:
        logger.exception(f"failed to open task folder: task_id={task_id}, error={e}")


@st.cache_resource
def init_log():
    # 基础日志 Handler 属于进程级资源，而不是页面会话状态。Streamlit 每次组件
    # 交互都会 rerun 页面脚本，代码热重载也可能让缓存失效。日志初始化只能
    # 精确替换终端 Handler，不能清空正在生成任务使用的 WebUI 临时 Handler。
    _lvl = get_logging_options()[0]

    return configure_terminal_logger(
        sys.stdout,
        level=_lvl,
        colorize=True,
    )


init_log()


def tr_optional(key, fallback_language=""):
    loc = locales.get(st.session_state["ui_language"], {})
    value = loc.get("Translation", {}).get(key, "")
    if not value and fallback_language:
        fallback_loc = locales.get(fallback_language, {})
        value = fallback_loc.get("Translation", {}).get(key, "")
    return value if value else ""


def render_onboarding_tour():
    # 引导只覆盖三个稳定入口，不尝试控制 Dialog、Tabs 或业务表单。这样既能让
    # 新用户理解完整流程，也不会把引导状态与 Streamlit 的动态组件生命周期耦合。
    steps = [
        Tour.bind(
            "open_settings_dialog_button",
            title=tr("Onboarding Model Settings Title"),
            desc=tr("Onboarding Model Settings Description"),
            side="bottom",
            align="end",
        ),
        Tour.bind(
            "main_settings_grid",
            title=tr("Onboarding Creation Settings Title"),
            desc=tr("Onboarding Creation Settings Description"),
            side="top",
            align="center",
        ),
        Tour.bind(
            "generate_video_button",
            title=tr("Onboarding Generate Video Title"),
            desc=tr("Onboarding Generate Video Description"),
            side="top",
            align="center",
        ),
    ]

    # streamlit-tour 1.1.0 没有在 Python 构造参数中暴露导航文案，但底层
    # Driver.js 支持在每一步的 popover 配置中覆盖按钮文本。这里统一注入本地化
    # 文案，并对内容做 HTML 转义，因为组件会通过 innerHTML 渲染这些字段。
    previous_text = html.escape(tr("Onboarding Previous"))
    next_text = html.escape(tr("Onboarding Next"))
    done_text = html.escape(tr("Onboarding Done"))
    for index, step in enumerate(steps):
        step.popover["prevBtnText"] = f"&larr; {previous_text}"
        # Driver.js 会在合并单步配置时覆盖已经替换过变量的进度模板，因此直接
        # 写入当前步骤和总步骤数，避免页面显示未解析的 {{current}} 占位符。
        step.popover["progressText"] = f"{index + 1} / {len(steps)}"
        if index == len(steps) - 1:
            step.popover["doneBtnText"] = done_text
        else:
            step.popover["nextBtnText"] = f"{next_text} &rarr;"

    tour = Tour(
        steps=steps,
        key=ONBOARDING_TOUR_KEY,
        show_progress=True,
        animate=True,
        overlay_opacity=0.55,
        one_time_tour=True,
    )

    # 每个 Streamlit 会话只主动启动一次。是否已经完成则由组件通过浏览器
    # localStorage 判断，避免页面 rerun 或普通控件交互反复弹出引导。
    auto_start_key = f"{ONBOARDING_TOUR_KEY}-auto-started"
    if not st.session_state.get(auto_start_key, False):
        st.session_state[auto_start_key] = True
        tour.start()


def _render_generation_logs(task_id):
    """渲染后台任务日志快照，不从工作线程访问 Streamlit 会话状态。"""
    if config.ui.get("hide_log", False):
        return

    log_records = webui_task.get_task_logs(task_id)
    if not log_records:
        return

    st.code("\n".join(log_records))


def _render_savage_stock_review(task_id):
    """Show cached Savage clips and collect explicit, hash-bound human review."""
    task_root = Path(utils.task_dir(task_id)).resolve()
    report_path = task_root / "top-panel-storyboard.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(report, list):
        return
    candidates = {}
    for entry in report:
        if (not isinstance(entry, dict)
                or entry.get("provider") not in material.STOCK_VIDEO_PROVIDER_PRIORITY
                or entry.get("approval_status") != "human_review_required"):
            continue
        provider = str(entry.get("provider"))
        asset_id, digest = str(entry.get("asset_id") or ""), str(entry.get("sha256") or "")
        if asset_id and re.fullmatch(r"[0-9a-f]{64}", digest):
            candidates.setdefault((provider, asset_id, digest), entry)
    if not candidates:
        return
    with st.expander("Stock footage review", expanded=False):
        st.caption(
            "Review the actual cached clip, source, license, and sampled frames. "
            "Approval is bound to this file's SHA-256 and does not change the already rendered video."
        )
        cache_root = Path(utils.storage_dir("cache_videos")).resolve()
        for index, ((provider, asset_id, digest), candidate) in enumerate(candidates.items()):
            candidate_path = Path(str(candidate.get("local_path") or "")).resolve()
            try:
                path_is_cached = os.path.commonpath([str(cache_root), str(candidate_path)]) == str(cache_root)
            except ValueError:
                path_is_cached = False
            if not path_is_cached or not candidate_path.is_file():
                continue
            try:
                if visual_review.sha256(candidate_path) != digest:
                    continue
            except OSError:
                continue
            approval = visual_review.human_approval(asset_id, digest)
            with st.container(border=True):
                st.write(f"{provider.title()} asset {asset_id} · {candidate.get('query') or 'saved keyword'}")
                st.caption(
                    f"License: {candidate.get('license') or 'unverified'} · "
                    f"SHA-256: {digest} · "
                    f"Status: {'approved' if approval else 'review required'}"
                )
                source_url = str(candidate.get("source_page") or "")
                allowed_source_prefixes = {
                    "pexels": ("https://www.pexels.com/",),
                    "pixabay": ("https://pixabay.com/",),
                    "coverr": ("https://coverr.co/", "https://www.coverr.co/"),
                }
                if source_url.startswith(allowed_source_prefixes.get(provider, ())):
                    st.link_button(
                        f"Open {provider.title()} source", source_url,
                        key=f"stock_source_{task_id}_{provider}_{asset_id}",
                    )
                st.video(str(candidate_path))
                frames = (candidate.get("visual_review") or {}).get("sampled_frames", [])
                columns = st.columns(min(3, max(1, len(frames))))
                for frame_index, frame in enumerate(frames):
                    relative = str(frame.get("path") or "")
                    frame_path = (task_root / relative).resolve()
                    try:
                        safe_frame = os.path.commonpath([str(task_root), str(frame_path)]) == str(task_root)
                    except ValueError:
                        safe_frame = False
                    if safe_frame and frame_path.is_file():
                        with columns[frame_index % len(columns)]:
                            st.image(str(frame_path), caption=f"Sample {frame_index + 1} · {frame.get('timestamp_seconds')}s")
                if approval:
                    st.caption(f"Approved by {approval['reviewer']}: {approval['reason']}")
                    continue
                approve_col, reject_col = st.columns(2)
                for column, status, label in (
                    (approve_col, "approved", "Approve this clip"),
                    (reject_col, "rejected", "Reject this clip"),
                ):
                    if column.button(label, key=f"stock_{status}_{task_id}_{provider}_{asset_id}"):
                        try:
                            visual_review.record_human_review(asset_id, digest, status)
                            st.success(f"Saved hash-bound review: {status}. Existing render is unchanged.")
                        except ValueError as exc:
                            st.error(str(exc))


def _render_generation_task_snapshot(task_id, task):
    """根据状态存储中的快照渲染进度、失败原因或最终成片。"""
    if not task:
        st.info(tr("Generating Video"))
        _render_generation_logs(task_id)
        return

    state = _normalize_task_state(task.get("state"))
    policy = task.get("fact_verification_policy", "advisory")
    raw_fact_state = task.get("fact_verification_state") or "unverified"
    fact_status = {
        "source_verified": "verified",
        "contradicted": "contradicted",
        "unavailable": "unavailable",
        "verification_error": "unavailable",
        "unsupported": "unverified",
        "unverified": "unverified",
    }.get(raw_fact_state, "unverified")
    st.caption(f"Fact check: {fact_status} · mode: {policy}")
    if task.get("unverified_publication_blocked"):
        st.warning("Automatic public upload was skipped because the narration is unverified and no publication opt-in is set.")
    if policy == "advisory" and fact_status in {"unverified", "contradicted", "unavailable"}:
        st.warning(
            f"Fact check is {fact_status}. Generation continues in Advisory mode; "
            "review the narration and sources before public use."
        )
    elif policy == "off":
        st.info("Fact checking was turned off; claims in this narration are unverified.")
    quality = task.get("alignment_quality") or {}
    subtitle_fallback = task.get("subtitle_alignment_fallback") or {}
    if subtitle_fallback.get("effective_mode") == "none":
        st.warning(
            "No narration subtitles were produced: alignment failed and local transcription was unavailable. "
            "Configure a usable local Whisper model or resolve pronunciation before retrying from saved audio."
        )
    elif subtitle_fallback.get("effective_mode") == "sentence":
        st.info("Word timing was unavailable; this video uses sentence subtitles.")
    if quality:
        st.caption(f"Hindi alignment: {quality.get('policy', 'balanced')} · "
                   f"{'accepted' if quality.get('accepted') else 'needs review'}")
        for warning in quality.get("warnings", []):
            st.warning(f"Uncertain word {warning.get('word_index')}: {warning.get('word')} · "
                       f"{warning.get('start')}–{warning.get('end')}s · score {warning.get('score')}")
        with st.expander("Alignment quality report"):
            st.json(quality)
    duration_metadata = task.get("duration_metadata") or {}
    if duration_metadata.get("actual_narration_duration_seconds") is not None:
        requested_min = duration_metadata.get("requested_min_duration_seconds")
        requested_max = duration_metadata.get("requested_max_duration_seconds")
        requested_text = (
            f"{requested_min if requested_min is not None else '—'}–"
            f"{requested_max if requested_max is not None else '—'}s"
        )
        actual_text = f"{duration_metadata['actual_narration_duration_seconds']:.2f}s narration"
        final_duration = duration_metadata.get("actual_final_video_duration_seconds")
        if final_duration is not None:
            actual_text += f" · {final_duration:.2f}s video"
        st.caption(
            f"Duration: requested {requested_text} · actual {actual_text} · "
            f"mode {duration_metadata.get('duration_enforcement', 'flexible')} · "
            f"outside range: {duration_metadata.get('duration_outside_requested_range')}"
        )
    progress = max(0, min(100, int(task.get("progress", 0) or 0)))
    if state == const.TASK_STATE_PROCESSING:
        st.info(tr("Generating Video"))
        st.progress(
            progress,
            text=f"{tr('Task Progress')}: {progress}%",
        )
        _render_generation_logs(task_id)
        return

    video_files = task.get("videos") or []
    if state != const.TASK_STATE_PROCESSING:
        _render_savage_stock_review(task_id)
    if state == const.TASK_STATE_FAILED:
        error = str(task.get("error") or "").strip()
        message = tr("Video Generation Failed")
        st.error(f"{message}: {error}" if error else message)
        try:
            audit_path = Path(utils.task_dir(task_id)) / "script.json"
            audit = json.loads(audit_path.read_text(encoding="utf-8")).get("visual_review_audit", [])
        except (OSError, ValueError, TypeError):
            audit = []
        if audit:
            with st.expander("Stock/ASMR visual review evidence"):
                st.caption("Review sampled frames from the actual usable clip range. Approval is saved locally by asset hash and applies to future tasks using the same source bytes.")
                for index, candidate in enumerate(audit):
                    with st.container(border=True):
                        st.write(
                            f"{candidate.get('provider')} · {candidate.get('asset_id')} · "
                            f"{candidate.get('status')} · {candidate.get('reason')}"
                        )
                        st.caption(
                            f"Search: {candidate.get('search_term') or '—'} · "
                            f"Source: {candidate.get('source_page') or '—'} · "
                            f"License: {candidate.get('license') or 'unverified'} · "
                            f"SHA-256: {candidate.get('sha256') or 'unavailable'}"
                        )
                        frames = candidate.get("sampled_frames", [])
                        image_columns = st.columns(min(3, max(1, len(frames))))
                        for frame_index, frame in enumerate(frames):
                            image_path = Path(utils.task_dir(task_id)) / str(frame.get("path", ""))
                            if image_path.is_file():
                                with image_columns[frame_index % len(image_columns)]:
                                    st.image(
                                        str(image_path),
                                        caption=(
                                            f"{frame.get('timestamp_seconds')}s · "
                                            f"{frame.get('width')}×{frame.get('height')} · "
                                            f"crop retention {frame.get('portrait_crop_retained_dimension_ratio')} · "
                                            f"edge score {frame.get('edge_contrast_score')}"
                                        ),
                                    )
                        review_cols = st.columns(2)
                        review_actions = (
                            ((review_cols[1], "rejected", "Reject footage"),)
                            if candidate.get("status") == "rejected"
                            else (
                                (review_cols[0], "approved", "Approve footage"),
                                (review_cols[1], "rejected", "Reject footage"),
                            )
                        )
                        for col, status, label in review_actions:
                            if col.button(label, key=f"visual_review_{status}_{task_id}_{index}"):
                                try:
                                    visual_review.record_human_review(
                                        str(candidate.get("asset_id") or ""),
                                        str(candidate.get("sha256") or ""), status,
                                    )
                                    st.success(f"Saved local visual review: {status}. Submit a new generation task to retry preflight.")
                                except ValueError as exc:
                                    st.error(str(exc))
        if not video_files:
            _render_generation_logs(task_id)
            return
    elif state != const.TASK_STATE_COMPLETE or not video_files:
        st.error(tr("Video Generation Failed"))
        _render_generation_logs(task_id)
        return

    if state == const.TASK_STATE_COMPLETE:
        st.success(tr("Video Generation Completed"))
    for warning in task.get("warnings") or []:
        if isinstance(warning, Mapping) and warning.get("code") == "batch_materials_reused":
            st.warning(
                tr("Batch Material Reuse Warning").format(
                    index=warning.get("video_index", ""), count=warning.get("count", 0)
                )
            )
        elif isinstance(warning, Mapping) and warning.get("code") == "sonilo_bgm_failed":
            st.warning(
                tr("Sonilo BGM Fallback Warning").format(
                    index=warning.get("video_index", "")
                )
            )
        elif (
            isinstance(warning, Mapping)
            and warning.get("code") == "elevenlabs_bgm_failed"
        ):
            st.warning(
                tr("ElevenLabs BGM Fallback Warning").format(
                    index=warning.get("video_index", "")
                )
            )
        else:
            st.warning(str(warning))

    available_videos = [
        (index, url)
        for index, url in enumerate(video_files)
        if os.path.isfile(url)
    ]
    for index, url in enumerate(video_files):
        if os.path.isfile(url):
            continue
        logger.warning(
            f"generated video is unavailable: "
            f"task_id={task_id}, video_file={url}"
        )

    try:
        if not available_videos:
            st.warning(tr("Generated Video Files Unavailable"))
        else:
            player_cols = st.columns(len(available_videos) * 2 + 1)
            for player_index, (video_index, url) in enumerate(available_videos):
                with player_cols[player_index * 2 + 1]:
                    st.video(url)
                    download_label = tr("Download Video")
                    if len(video_files) > 1:
                        download_label = f"{download_label} {video_index + 1}"
                    download_name = _build_video_download_name(
                        task.get("video_subject"),
                        video_index + 1,
                        len(video_files),
                    )
                    with open(url, "rb") as video_file:
                        st.download_button(
                            download_label,
                            data=video_file,
                            file_name=download_name,
                            mime=mimetypes.guess_type(url)[0] or "video/mp4",
                            key=f"download_generated_video_{task_id}_{video_index}",
                            icon=":material/download:",
                            on_click="ignore",
                            width="stretch",
                        )
    except Exception as exc:
        logger.exception(
            f"failed to render generated video preview: task_id={task_id}, "
            f"video_files={video_files}, error={exc}"
        )

    _render_generation_logs(task_id)
    if (
        state == const.TASK_STATE_COMPLETE
        and st.session_state.get("handled_generation_task_id") != task_id
    ):
        # Fragment 可能重复渲染同一个完成任务。无论是否开启自动打开目录，
        # 每个任务都只处理一次完成事件，避免重复弹出资源管理器或重复写入日志。
        st.session_state["handled_generation_task_id"] = task_id
        if config.ui.get("open_task_folder_on_completion", True):
            open_task_folder(task_id)
        logger.info(f"{tr('Video Generation Completed')}: task_id={task_id}")


@st.fragment(run_every=webui_task.TASK_LOG_REFRESH_INTERVAL_SECONDS)
def _render_running_generation_task(task_id):
    """只在任务运行期间轮询；结束后切回静态结果，停止不必要的定时刷新。"""
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.exception(
            f"failed to query WebUI generation task: task_id={task_id}, error={exc}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state = _normalize_task_state((task or {}).get("state"))
    if state in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _remove_active_generation_task(task_id)
        # 完整页面脚本现在没有耗时生成逻辑，可以安全 rerun 并把结果改为静态
        # 渲染。这样任务结束后不会让浏览器永久保留一个两秒轮询的 Fragment。
        st.rerun(scope="app")

    _render_generation_task_snapshot(task_id, task)


def _render_current_generation_task():
    """在生成按钮下方恢复当前页面最近提交任务的可查询 UI。"""
    task_id = st.session_state.get("current_generation_task_id", "")
    if not task_id:
        return

    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.exception(
            f"failed to query current WebUI task: task_id={task_id}, error={exc}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state = _normalize_task_state((task or {}).get("state"))
    if state in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _remove_active_generation_task(task_id)
        _render_generation_task_snapshot(task_id, task)
        return

    _render_running_generation_task(task_id)


def get_llm_provider_tips(provider_id, **kwargs):
    # LLM provider 说明文案统一使用 `llm_provider_tips.<provider_id>` 规则。
    # 这样新增 provider 时只需要在 locale 中补文案；没有文案时不展示提示块，
    # 避免 Main.py 里继续堆叠大量中英文硬编码说明。
    provider = get_llm_provider(provider_id)
    if provider is None:
        return ""

    # Provider 配置说明目前统一维护中文和英文两套规范模板；其它界面语言
    # 统一使用英文，避免在 locale 中复制英文后长期不同步。后续某个语种完成
    # 全量翻译后，再将它加入这里的独立维护范围。
    ui_language = st.session_state.get("ui_language", "en")
    tips_language = ui_language if ui_language in {"zh", "en"} else "en"
    tips = (
        locales.get(tips_language, {}).get("Translation", {}).get(provider.tips_key, "")
    )
    if not tips:
        return tips

    service_endpoint = provider.preferred_service_endpoint(
        prefer_international=tips_language == "en"
    )
    api_key_url = (
        service_endpoint.api_key_url
        if service_endpoint
        else provider.effective_api_key_url()
    )
    format_context = {
        "api_key_url": api_key_url,
        "default_model": provider.default_model,
        "default_base_url": (
            service_endpoint.base_url
            if service_endpoint
            else provider.effective_default_base_url
        ),
        "model_docs_url": (
            service_endpoint.model_docs_url
            if service_endpoint and service_endpoint.model_docs_url
            else provider.effective_model_docs_url(
                prefer_international=tips_language == "en"
            )
        ),
        **{
            f"default_{field.config_suffix}": field.default_value
            for field in provider.extra_fields
        },
        **kwargs,
    }
    try:
        return tips.format(**format_context)
    except Exception as e:
        logger.warning(f"format llm provider tips failed: {provider_id}, {e}")
        return tips


def format_llm_connection_error(provider_id, base_url, error):
    """为可明确定位的鉴权错误补充配置检查建议，同时保留原始响应。"""
    error_text = str(error or "").strip()
    normalized_error = error_text.lower()
    authentication_markers = (
        "401",
        "authentication",
        "invalid api key",
        "invalid_api_key",
        "unauthorized",
    )
    provider = get_llm_provider(provider_id)
    if provider is None or not provider.service_endpoints or not any(
        marker in normalized_error for marker in authentication_markers
    ):
        return error_text

    message = tr_optional(
        provider.authentication_error_key,
        fallback_language="en",
    )
    if not message:
        return error_text
    return message.format(base_url=base_url or "-", error=error_text)


def get_llm_provider_label(provider):
    return tr_optional(provider.label_key) or provider.default_label


def get_tts_provider_tips(provider_id):
    # TTS 配置说明与 LLM Provider 采用相同维护策略：只维护中英文，
    # 其它界面语言统一回退英文，避免复制后长期不同步。
    ui_language = st.session_state.get("ui_language", "en")
    tips_language = ui_language if ui_language in {"zh", "en"} else "en"
    return (
        locales.get(tips_language, {})
        .get("Translation", {})
        .get(f"tts_provider_tips.{provider_id}", "")
    )


def localized_widget_key(name, *parts):
    # 部分 Streamlit selectbox 使用稳定 key 记住选择状态，但展示文本来自 locale。
    # 语言切换时把语言也放进 key，可以强制重建控件，避免选中项仍显示旧语言。
    language = st.session_state.get("ui_language", config.ui.get("language", ""))
    suffix_parts = [name, language, *[str(part) for part in parts if part]]
    return "_".join(suffix_parts)


def stable_selectbox(label, options, default_value, key, format_func=None, **kwargs):
    # Streamlit 1.59 对 selectbox 的状态复用更敏感：如果控件没有固定 key，
    # 或者真实选项只是一组临时下标，页面 rerun 后容易被重新计算的 index 覆盖，
    # 表现为用户第一次选择不生效、需要再选一次。这个 helper 统一用稳定业务值
    # 作为真实选项，并在 session_state 里保存该值；展示文案只通过 format_func
    # 转换，避免翻译文案、选项顺序或上游配置变化影响选择状态。
    options = list(options)
    if not options:
        raise ValueError(f"selectbox options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    selected_value = st.session_state.get(widget_key)
    accepts_custom_value = bool(kwargs.get("accept_new_options"))
    has_valid_custom_value = (
        accepts_custom_value
        and isinstance(selected_value, str)
        and bool(selected_value.strip())
    )
    if selected_value not in options and not has_valid_custom_value:
        # 如果上游选项发生变化（例如切换 TTS provider 后声音列表变了），
        # 旧值已经不合法。控件创建前直接初始化 session_state，之后只让 key
        # 管理状态，不再同时传入 index。这样可以避免 Streamlit 在 rerun 时
        # 用重新计算的 index 覆盖用户刚选择的值，导致第一次选择不生效。
        st.session_state[widget_key] = default_value

    if format_func is None:
        format_func = str

    return st.selectbox(
        label,
        options=options,
        format_func=format_func,
        key=widget_key,
        **kwargs,
    )


# Streamlit 原生 selectbox 暂不支持 HTML optgroup。这里使用 1.59 自带的
# Components v2 封装原生 <select>/<optgroup>，无需引入前端依赖，同时保留浏览器
# 原生的键盘导航、无障碍语义和移动端选择体验。组件只传递固定业务值和翻译文本，
# 不接收任意 HTML，从边界上避免配置内容进入 innerHTML。
_GROUPED_SELECT_COMPONENT = st.components.v2.component(
    "mpt_grouped_select",
    html="""
        <div class="mpt-grouped-select">
            <div class="mpt-grouped-select__label-row">
                <label class="mpt-grouped-select__label"></label>
                <button class="mpt-grouped-select__settings" type="button"></button>
            </div>
            <div class="mpt-grouped-select__control">
                <select></select>
            </div>
        </div>
    """,
    css="""
        .mpt-grouped-select {
            width: 100%;
            color: var(--st-text-color);
            font-family: var(--st-font);
        }

        .mpt-grouped-select__label-row {
            display: flex;
            flex-wrap: wrap;
            align-items: baseline;
            gap: 0.45rem;
            margin-bottom: 0.35rem;
        }

        .mpt-grouped-select__label {
            font-size: 0.875rem;
            line-height: 1.25rem;
        }

        .mpt-grouped-select__settings {
            padding: 0;
            border: 0;
            background: transparent;
            color: var(--st-link-color);
            font: inherit;
            font-size: 0.8rem;
            line-height: 1.25rem;
            cursor: pointer;
        }

        .mpt-grouped-select__settings:hover {
            text-decoration: underline;
            text-underline-offset: 0.15rem;
        }

        .mpt-grouped-select__settings:focus-visible {
            border-radius: 0.2rem;
            outline: 2px solid var(--st-primary-color);
            outline-offset: 2px;
        }

        .mpt-grouped-select__control {
            position: relative;
        }

        .mpt-grouped-select__control::after {
            position: absolute;
            top: 50%;
            right: 1rem;
            width: 0.55rem;
            height: 0.55rem;
            border-right: 2px solid currentColor;
            border-bottom: 2px solid currentColor;
            content: "";
            pointer-events: none;
            transform: translateY(-70%) rotate(45deg);
        }

        .mpt-grouped-select select {
            width: 100%;
            min-height: 2.5rem;
            padding: 0.45rem 2.75rem 0.45rem 0.75rem;
            border: 1px solid color-mix(in srgb, currentColor 20%, transparent);
            border-radius: 0.5rem;
            outline: none;
            appearance: none;
            background: var(--st-secondary-background-color);
            color: inherit;
            font: inherit;
            cursor: pointer;
        }

        .mpt-grouped-select select:hover {
            border-color: color-mix(in srgb, currentColor 36%, transparent);
        }

        .mpt-grouped-select select:focus-visible {
            border-color: var(--st-primary-color);
            box-shadow: 0 0 0 1px var(--st-primary-color);
        }
    """,
    js="""
        export default function(component) {
            const { data, parentElement, setTriggerValue } = component;
            const label = parentElement.querySelector("label");
            const settings = parentElement.querySelector(".mpt-grouped-select__settings");
            const select = parentElement.querySelector("select");

            label.textContent = data.label;
            settings.textContent = data.settingsLabel;
            settings.hidden = !data.settingsLabel;
            select.id = data.controlId;
            label.htmlFor = data.controlId;
            select.setAttribute("aria-label", data.label);
            select.replaceChildren();

            for (const groupData of data.groups) {
                const group = document.createElement("optgroup");
                group.label = groupData.label;
                for (const optionData of groupData.options) {
                    const option = document.createElement("option");
                    option.value = optionData.value;
                    option.textContent = optionData.label;
                    group.appendChild(option);
                }
                select.appendChild(group);
            }

            select.value = data.value;
            const handleChange = () => {
                setTriggerValue("selected", select.value);
            };
            const handleSettings = () => {
                setTriggerValue("settings", true);
            };
            select.addEventListener("change", handleChange);
            settings.addEventListener("click", handleSettings);

            return () => {
                select.removeEventListener("change", handleChange);
                settings.removeEventListener("click", handleSettings);
            };
        }
    """,
)


def grouped_selectbox(
    label,
    groups,
    default_value,
    key,
    format_func=None,
    settings_label="",
    on_settings=None,
):
    """渲染带不可选分组标题的单个下拉框，并返回稳定业务值。"""
    if format_func is None:
        format_func = str

    normalized_groups = []
    valid_values = []
    for group_label, options in groups:
        normalized_options = []
        for option in options:
            valid_values.append(option)
            normalized_options.append(
                {"value": option, "label": str(format_func(option))}
            )
        if normalized_options:
            normalized_groups.append(
                {"label": str(group_label), "options": normalized_options}
            )

    if not valid_values:
        raise ValueError(f"grouped selectbox options cannot be empty: {key}")
    if len(set(valid_values)) != len(valid_values):
        raise ValueError(f"grouped selectbox options must be unique: {key}")
    if default_value not in valid_values:
        default_value = valid_values[0]

    # 业务选择保存在与旧 selectbox 相同的 session key 中，设置预设恢复和
    # 语言切换逻辑无需分叉；组件自身使用独立 key，避免与业务状态冲突。
    widget_key = localized_widget_key(key)
    if widget_key not in st.session_state:
        st.session_state[widget_key] = default_value
    selected_value = st.session_state[widget_key]
    if selected_value not in valid_values:
        selected_value = default_value
        st.session_state[widget_key] = selected_value

    result = _GROUPED_SELECT_COMPONENT(
        key=f"{widget_key}_component",
        data={
            "label": label,
            "settingsLabel": settings_label,
            # 显式关联可见 label 与原生 select。组件 key 由固定业务名称和
            # 语言代码组成，在页面内唯一，既方便鼠标点击标签聚焦控件，
            # 也不会引入随机 ID 导致每次 rerun 都重建前端状态。
            "controlId": f"{widget_key}_control",
            "value": selected_value,
            "groups": normalized_groups,
        },
        on_selected_change=lambda: None,
        on_settings_change=on_settings or (lambda: None),
    )
    changed_value = getattr(result, "selected", None)
    if changed_value in valid_values and changed_value != selected_value:
        st.session_state[widget_key] = changed_value
        # Components v2 在当前脚本轮次返回事件时，本轮传给前端的 data 仍是
        # 事件发生前的旧值。立即自动 rerun，让组件和依赖 video_source 的控件
        # 同时收到新值；否则下拉框会被旧 data 短暂覆盖，用户只能再选一次。
        st.rerun()

    return selected_value


def sync_script_order_concat_mode():
    """在文案顺序匹配开启时固定使用顺序拼接，并在关闭后恢复原选择。"""
    widget_key = localized_widget_key("video_concat_mode_select")
    previous_key = "video_concat_mode_before_script_order_match"
    match_script_order = bool(st.session_state.get("match_materials_to_script", False))

    if match_script_order:
        current_mode = st.session_state.get(widget_key, VideoConcatMode.random.value)
        if current_mode != VideoConcatMode.sequential.value:
            st.session_state[previous_key] = current_mode
        st.session_state[widget_key] = VideoConcatMode.sequential.value
        return

    previous_mode = st.session_state.pop(previous_key, None)
    if previous_mode in {
        VideoConcatMode.sequential.value,
        VideoConcatMode.random.value,
    }:
        st.session_state[widget_key] = previous_mode


def reset_script_system_prompt():
    """将高级脚本设置中的系统提示词恢复为当前版本的默认内容。"""
    st.session_state["custom_system_prompt"] = llm.DEFAULT_SCRIPT_SYSTEM_PROMPT


def _on_visual_style_changed():
    """Apply the selected style's visible defaults before the next UI render."""
    selected_label = st.session_state.get("visual_style_select")
    selected_style = VISUAL_STYLE_VALUES.get(selected_label, "auto")
    st.session_state["_visual_style_user_selected"] = True
    previous_style = st.session_state.get("_active_visual_style_preset", "auto")
    force_defaults = st.session_state.pop("_force_style_preset_reset", False)
    if selected_style == previous_style and not force_defaults:
        return

    if "_neutral_tts_defaults" not in st.session_state:
        st.session_state["_neutral_tts_defaults"] = {
            "tts_server": config.ui.get("tts_server", "azure-tts-v1"),
            "voice_name": config.ui.get("voice_name", ""),
            "gemini_tts_model": config.ui.get(
                "gemini_tts_model", "gemini-3.8-flash-tts"
            ),
        }

    defaults = visual_style_service.preset_for(selected_style).defaults
    for field in ("narrator_preset", "profanity_level"):
        value = defaults.get(field)
        if value is not None:
            _set_stable_widget_value(
                "narrator_preset_select" if field == "narrator_preset"
                else "profanity_level_select",
                value,
            )
            _set_runtime_config("ui", field, value)

    display_mode = defaults.get("caption_preset")
    if display_mode:
        _set_stable_widget_value("subtitle_display_mode_select", display_mode)
        _set_runtime_config("ui", "subtitle_display_mode", display_mode)
    animation = defaults.get("subtitle_animation")
    if animation:
        _set_stable_widget_value("subtitle_animation_select", animation)
        _set_runtime_config("ui", "subtitle_animation", animation)
    color = defaults.get("text_fore_color")
    if color:
        st.session_state["font_color_picker"] = color
        _set_runtime_config("ui", "text_fore_color", color)

    if selected_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER:
        if config.app.get("gemini_api_key"):
            _set_stable_widget_value("tts_server_select", "gemini-tts")
            _set_stable_widget_value("voice_mode_control", VOICE_MODE_TTS)
            _set_stable_widget_value(
                "speech_synthesis_select_gemini-tts", "gemini:Puck-Upbeat"
            )
            _set_runtime_config("ui", "tts_server", "gemini-tts")
            _set_runtime_config("ui", "gemini_tts_model", "gemini-3.8-flash-tts")
            _set_runtime_config("ui", "voice_name", "gemini:Puck-Upbeat")
        language_key = localized_widget_key("script_language_select")
        if not st.session_state.get(language_key):
            _set_stable_widget_value("script_language_select", "hi")
    elif (previous_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER
          or force_defaults):
        baseline = st.session_state["_neutral_tts_defaults"]
        _set_stable_widget_value("tts_server_select", baseline["tts_server"])
        _set_runtime_config("ui", "tts_server", baseline["tts_server"])
        _set_stable_widget_value(
            f"speech_synthesis_select_{baseline['tts_server']}",
            baseline["voice_name"],
        )
        _set_runtime_config("ui", "voice_name", baseline["voice_name"])
        _set_runtime_config(
            "ui", "gemini_tts_model", baseline["gemini_tts_model"]
        )

    script = str(st.session_state.get("video_script", "") or "").strip()
    st.session_state["video_script_needs_style_review"] = bool(script)
    st.session_state["_active_visual_style_preset"] = selected_style


def _reset_visual_style_defaults():
    st.session_state["_force_style_preset_reset"] = True
    _on_visual_style_changed()


def reset_subtitle_settings():
    """恢复 WebUI 字幕控件和持久化配置中的默认值。"""
    defaults = DEFAULT_SUBTITLE_SETTINGS
    st.session_state["subtitle_enabled_checkbox"] = defaults["subtitle_enabled"]
    _set_stable_widget_value("font_name_select", defaults["font_name"])
    _set_stable_widget_value("subtitle_position_select", defaults["subtitle_position"])
    _set_stable_widget_value(
        "subtitle_display_mode_select", defaults["subtitle_display_mode"]
    )
    _set_stable_widget_value(
        "subtitle_animation_select", defaults["subtitle_animation"]
    )
    st.session_state["custom_position_input"] = str(defaults["custom_position"])
    st.session_state["font_color_picker"] = defaults["text_fore_color"]
    st.session_state["font_size_slider"] = defaults["font_size"]
    st.session_state["stroke_color_picker"] = defaults["stroke_color"]
    st.session_state["stroke_width_slider"] = defaults["stroke_width"]
    st.session_state["subtitle_background_enabled_checkbox"] = defaults[
        "subtitle_background_enabled"
    ]
    st.session_state["subtitle_background_color_picker"] = defaults[
        "subtitle_background_color"
    ]
    st.session_state["rounded_subtitle_background_checkbox"] = defaults[
        "rounded_subtitle_background"
    ]

    # 同步会持久化的 UI 选项，确保恢复后刷新页面仍保持默认设置。
    for key in (
        "subtitle_enabled",
        "font_name",
        "subtitle_position",
        "subtitle_display_mode",
        "subtitle_animation",
        "custom_position",
        "text_fore_color",
        "font_size",
        "stroke_color",
        "stroke_width",
        "subtitle_background_enabled",
        "subtitle_background_color",
        "rounded_subtitle_background",
    ):
        if key in defaults:
            _set_runtime_config("ui", key, defaults[key])


@st.dialog(tr("Final Prompt Preview"), width="large")
def render_script_prompt_preview(prompt):
    """展示将要发送给大模型的完整脚本生成提示词。"""
    st.code(prompt, language="markdown", wrap_lines=True)


def stable_segmented_control(
    label, options, default_value, key, format_func=None, **kwargs
):
    """使用稳定业务值创建单选分段控件，避免语言切换后状态被展示文案覆盖。"""
    options = list(options)
    if not options:
        raise ValueError(f"segmented control options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    if st.session_state.get(widget_key) not in options:
        st.session_state[widget_key] = default_value

    return st.segmented_control(
        label,
        options=options,
        selection_mode="single",
        required=True,
        format_func=format_func or str,
        key=widget_key,
        **kwargs,
    )


@st.cache_data(ttl=300, show_spinner=False)
def get_groq_model_ids(api_key: str, base_url: str) -> list[str]:
    if not api_key:
        return []

    normalized_base_url = (
        (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
    )
    models_url = f"{normalized_base_url}/models"

    try:
        response = trace_http_call(
            "GET", requests.get,
            models_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", [])

        model_ids = []
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    model_ids.append(model_id.strip())

        return sorted(set(model_ids))
    except Exception as e:
        logger.warning(f"failed to fetch groq models: {e}")
        return []


def _get_material_api_keys(config_key):
    """将配置中的素材 API Key 统一转换为 WebUI 可编辑字符串。"""
    api_keys = config.app.get(config_key, [])
    if isinstance(api_keys, str):
        api_keys = [api_keys]
    return ", ".join(api_keys)


def _save_material_api_keys(config_key, value):
    """保存逗号分隔的素材 API Key，并允许用户显式清空旧配置。"""
    normalized_value = value.replace(" ", "")
    _set_runtime_config(
        "app",
        config_key,
        normalized_value.split(",") if normalized_value else [],
    )


def _format_file_size(size_bytes):
    """将字节数格式化为适合设置页展示的紧凑容量文本。"""
    size = float(max(0, size_bytes))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


@st.cache_data(ttl=30, show_spinner=False)
def _get_video_cache_stats_data(max_age_days=None):
    """
    短周期缓存目录统计，避免设置弹窗内普通控件交互反复扫描大量文件。

    缓存键包含清理天数，因此切换范围只会为每个范围扫描一次；主动刷新或清理
    完成后会显式清空，最多 30 秒的缓存不会影响实际删除时的二次扫描。

    这里只缓存纯 dict，而不是 VideoCacheStats 实例：st.cache_data 需要用 pickle
    序列化返回值，而 pickle 保存自定义类时按「模块名 + 类名」引用，并校验解析
    出来的类与实例的类是同一个对象。Streamlit 源码监视器在本地源码文件发生变化
    时（包含 Windows 上防病毒或索引器引起的偶发事件）会清空 sys.modules 中的
    被监视模块并重新导入；此时仍打开的设置弹窗（Dialog 继承 fragment 行为，
    内部交互不会重建 Main.py 顶层引用）持有的还是旧模块里的类，pickle 会抛
    PicklingError: it's not the same object as ...，并被 Streamlit 包装成
    UnserializableReturnValueError（对应 Streamlit 官方 issue #14593 的已知缺陷）。
    纯 dict 按值序列化、不含类引用，因此不受模块重新导入影响。
    """
    stats = cache_manager.get_video_cache_stats(max_age_days=max_age_days)
    # 字段名与 VideoCacheStats 完全一致，供下方 _get_video_cache_stats 直接展开
    # 还原；今后若 VideoCacheStats 增删字段，需要同步维护这里的字段映射。
    return {
        "file_count": stats.file_count,
        "total_size": stats.total_size,
        "oldest_mtime": stats.oldest_mtime,
        "newest_mtime": stats.newest_mtime,
    }


def _get_video_cache_stats(max_age_days=None) -> cache_manager.VideoCacheStats:
    """在缓存边界之外把纯数据还原为 VideoCacheStats，调用方属性访问方式不变。"""

    # 还原时使用当前生效的 cache_manager 模块；即使模块被重新导入，也只是重建
    # 一个轻量 dataclass，不会再触发 pickle 的「类引用身份」校验。
    return cache_manager.VideoCacheStats(
        **_get_video_cache_stats_data(max_age_days=max_age_days)
    )


def _render_cache_management_settings(panel):
    """渲染默认在线视频素材缓存的统计、预览和安全清理操作。"""
    with panel:
        cleanup_message = st.session_state.pop("video_cache_cleanup_message", None)
        if cleanup_message:
            message_type, message = cleanup_message
            if message_type == "success":
                st.success(message)
            else:
                st.warning(message)

        st.caption(tr("Video Cache Directory"))
        st.code(cache_manager.video_cache_dir(), language="text")

        total_stats = _get_video_cache_stats()
        metric_count, metric_size, metric_oldest = st.columns(3)
        metric_count.metric(tr("Cache File Count"), total_stats.file_count)
        metric_size.metric(
            tr("Cache Total Size"), _format_file_size(total_stats.total_size)
        )
        oldest_text = (
            datetime.fromtimestamp(total_stats.oldest_mtime).strftime("%Y-%m-%d")
            if total_stats.oldest_mtime is not None
            else "-"
        )
        metric_oldest.metric(tr("Oldest Cache Date"), oldest_text)

        st.caption(tr("Video Cache Management Help"))
        cleanup_options = (30, 7, 90, None)
        cleanup_labels = {
            30: tr("Cache Older Than 30 Days"),
            7: tr("Cache Older Than 7 Days"),
            90: tr("Cache Older Than 90 Days"),
            None: tr("All Video Cache"),
        }
        max_age_days = st.selectbox(
            tr("Cache Cleanup Range"),
            options=cleanup_options,
            format_func=lambda value: cleanup_labels[value],
            key="video_cache_cleanup_range",
        )
        cleanup_preview = _get_video_cache_stats(max_age_days=max_age_days)
        st.info(
            tr("Cache Cleanup Preview").format(
                count=cleanup_preview.file_count,
                size=_format_file_size(cleanup_preview.total_size),
            )
        )

        confirm_nonce = st.session_state.get("video_cache_cleanup_confirm_nonce", 0)
        confirmed = st.checkbox(
            tr("Confirm Cache Cleanup"),
            key=f"video_cache_cleanup_confirm_{confirm_nonce}",
        )
        refresh_col, open_col, cleanup_col = st.columns(3)
        if refresh_col.button(
            tr("Refresh Cache Stats"),
            key="refresh_video_cache_stats",
            width="stretch",
            icon=":material/refresh:",
        ):
            _get_video_cache_stats_data.clear()
            st.rerun(scope="fragment")

        if open_col.button(
            tr("Open Cache Directory"),
            key="open_video_cache_directory",
            width="stretch",
            icon=":material/folder_open:",
        ):
            webbrowser.open(Path(cache_manager.video_cache_dir()).as_uri())

        cleanup_disabled = not confirmed or cleanup_preview.file_count == 0
        if cleanup_col.button(
            tr("Clean Cache Now"),
            key="clean_video_cache_now",
            type="primary",
            disabled=cleanup_disabled,
            width="stretch",
            icon=":material/delete_sweep:",
        ):
            result = cache_manager.clean_video_cache(max_age_days=max_age_days)
            message_key = (
                "Cache Cleanup Completed With Failures"
                if result.failed_count
                else "Cache Cleanup Completed"
            )
            st.session_state["video_cache_cleanup_message"] = (
                "warning" if result.failed_count else "success",
                tr(message_key).format(
                    count=result.deleted_count,
                    size=_format_file_size(result.deleted_size),
                    failed=result.failed_count,
                ),
            )
            # Streamlit 不允许在控件实例化后修改同名 session_state。通过递增
            # nonce 让下一次 fragment rerun 创建未勾选的新控件，避免清理完成后
            # 危险确认状态被继续保留。
            st.session_state["video_cache_cleanup_confirm_nonce"] = confirm_nonce + 1
            _get_video_cache_stats_data.clear()
            st.rerun(scope="fragment")


# -----------------------------------------------------------------------------
# 设置预设导出导入与密钥备份
# -----------------------------------------------------------------------------


def _is_credential_config_key(key):
    """判断一个配置项名称是否表示凭据。"""
    return str(key).endswith(CREDENTIAL_KEY_SUFFIXES)


def _is_backup_config_key(section_name, key):
    """凭据本身及其配套配置项都属于密钥备份范围。"""
    if _is_credential_config_key(key):
        return True
    if key in CREDENTIAL_COMPANION_KEYS.get(section_name, ()):
        return True
    return key in NON_LLM_COMPANION_KEYS.get(section_name, ())


def _credential_widget_state_keys(section_name, key):
    """
    返回某个凭据配置项对应的全部 Streamlit 控件 key。

    密码输入框都带 key，Streamlit 中 session_state 的值优先于控件的 value
    参数。恢复备份后必须清除这些残留控件状态，否则页面会继续显示旧密钥，
    并在下一次 rerun 把旧值重新写回配置，让恢复看起来没有生效。多个面板
    共用同一个密钥时会各自持有控件状态，因此返回默认 key 和全部别名。
    """
    if section_name == "app":
        default_widget_key = f"{key}_input"
    else:
        default_widget_key = f"{section_name}_{key}_input"
    return (
        default_widget_key,
        *CREDENTIAL_WIDGET_STATE_ALIASES.get((section_name, key), ()),
    )


def _normalize_backup_value(value):
    """归一化备份值，丢弃空字符串和空列表，避免恢复时覆盖成空配置。"""
    if isinstance(value, list):
        items = [
            str(item).strip()
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        return items or None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _collect_key_backup(config_sections):
    """从运行期配置分区中收集所有已填写的密钥及其配套配置项。"""
    backup = {}
    for section_name, section in config_sections.items():
        if section_name in KEY_BACKUP_EXCLUDED_SECTIONS:
            continue
        entries = {}
        for key, value in section.items():
            if not _is_backup_config_key(section_name, key):
                continue
            normalized_value = _normalize_backup_value(value)
            if normalized_value is not None:
                entries[key] = normalized_value
        if entries:
            backup[section_name] = entries
    return backup


def _count_backup_keys(backup):
    """统计备份中的配置项数量，用于界面提示和禁用空导出。"""
    return sum(len(entries) for entries in backup.values())


def _build_key_backup_payload(config_sections, app_version):
    """构造密钥备份文件内容。"""
    return {
        "schema": KEY_BACKUP_SCHEMA,
        "version": KEY_BACKUP_VERSION,
        "app_version": str(app_version),
        "keys": _collect_key_backup(config_sections),
    }


def _load_transfer_payload(raw_bytes, schema, version):
    """
    解析导出文件，并校验它确实来自本功能的同一版本。

    用户可能上传任意 JSON。这里只接受声明了正确 schema 和版本的文件，让错误
    提示停留在导入入口，而不是把无法识别的内容写进配置或控件状态。
    Windows 编辑器可能保存带 BOM 的 JSON，因此按 utf-8-sig 解码。
    """
    if len(raw_bytes) > MAX_SETTINGS_TRANSFER_BYTES:
        raise ValueError("settings import exceeds the 2 MB limit")
    payload = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("exported file must contain a JSON object")
    if payload.get("schema") != schema:
        raise ValueError(f"unexpected schema: {payload.get('schema')!r}")
    if payload.get("version") != version:
        raise ValueError(f"unsupported version: {payload.get('version')!r}")
    return payload


def _parse_key_backup(raw_bytes, config_sections):
    """
    解析密钥备份文件，只保留当前版本认识的分区和配置项。

    备份文件可以手工编辑，也可能来自更新的版本。未知分区或非密钥配置项一律
    忽略，避免通过导入功能改写与凭据无关的配置。
    """
    payload = _load_transfer_payload(raw_bytes, KEY_BACKUP_SCHEMA, KEY_BACKUP_VERSION)
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("key backup file has no keys object")

    restored = {}
    for section_name, entries in keys.items():
        if section_name not in config_sections:
            continue
        if section_name in KEY_BACKUP_EXCLUDED_SECTIONS:
            continue
        if not isinstance(entries, dict):
            continue
        section_entries = {}
        for key, value in entries.items():
            if not _is_backup_config_key(section_name, key):
                continue
            normalized_value = _normalize_backup_value(value)
            if normalized_value is not None:
                section_entries[key] = normalized_value
        if section_entries:
            restored[section_name] = section_entries

    if not restored:
        raise ValueError("key backup file contains no restorable keys")
    return restored


def _build_settings_preset_payload(params, app_version):
    """构造生成参数预设文件内容。"""
    preset_params = {
        key: value
        for key, value in params.items()
        if key not in PRESET_EXCLUDED_PARAM_KEYS
    }
    if params.get("bgm_type") == "preset" and params.get("bgm_file"):
        try:
            builtin_bgm_path = bgm_service.resolve_builtin_bgm_file(
                str(params["bgm_file"])
            )
        except ValueError:
            # 自定义文件属于本机资源，不能进入可移植的设置预设。异常场景下保持
            # 既有排除行为，避免导出文件包含绝对路径或另一台设备不存在的 UUID。
            pass
        else:
            preset_params["bgm_file"] = Path(builtin_bgm_path).name
    return {
        "schema": SETTINGS_PRESET_SCHEMA,
        "version": SETTINGS_PRESET_VERSION,
        "app_version": str(app_version),
        "params": preset_params,
    }


def _parse_settings_preset(raw_bytes):
    """
    解析预设文件并交给 VideoParams 校验。

    预设可以在其它机器上生成，也可能被手工编辑。统一走模型校验可以复用既有
    的取值范围约束，非法预设在导入时就被拒绝，而不是在生成任务时才失败。
    """
    payload = _load_transfer_payload(
        raw_bytes, SETTINGS_PRESET_SCHEMA, SETTINGS_PRESET_VERSION
    )
    preset_params = payload.get("params")
    if not isinstance(preset_params, dict):
        raise ValueError("settings preset file has no params object")

    params_input = {
        key: value
        for key, value in preset_params.items()
        if key not in PRESET_EXCLUDED_PARAM_KEYS
    }
    if preset_params.get("bgm_type") == "preset" and preset_params.get("bgm_file"):
        # 设置预设只能恢复当前版本真实存在的内置歌曲。服务层同时拒绝目录分隔符
        # 和用户上传文件，防止导入文件借试听功能读取任意本机路径。
        builtin_bgm_path = bgm_service.resolve_builtin_bgm_file(
            str(preset_params["bgm_file"])
        )
        params_input["bgm_file"] = Path(builtin_bgm_path).name
    # video_subject 是 VideoParams 的必填字段，但预设允许只保存风格设置。
    params_input.setdefault("video_subject", "")
    return VideoParams.model_validate(params_input).model_dump(mode="json")


def _apply_key_backup(restored_keys):
    """把解析后的密钥写回运行期配置，并清除对应控件的残留状态。"""
    restored_count = 0
    for section_name, entries in restored_keys.items():
        for key, value in entries.items():
            _set_runtime_config(section_name, key, value)
            for widget_key in _credential_widget_state_keys(section_name, key):
                st.session_state.pop(widget_key, None)
            restored_count += 1
    # ElevenLabs 音色列表按密钥缓存，换用另一份备份后必须重新拉取。
    for cache_key in list(st.session_state.keys()):
        if str(cache_key).startswith("elevenlabs_voices_"):
            del st.session_state[cache_key]
    return restored_count


def _apply_pending_settings_preset():
    """在渲染任何控件之前应用已导入的预设。"""
    preset_params = st.session_state.pop("settings_preset_payload", None)
    if not preset_params:
        return False

    _apply_restored_params(preset_params)
    logger.info("applied imported settings preset")
    return True


def _render_settings_transfer(params):
    """渲染生成参数预设的导出与导入入口。"""
    with st.expander(tr("Settings Preset"), expanded=False):
        st.caption(tr("Settings Preset Help"))
        preset_payload = _build_settings_preset_payload(
            params.model_dump(mode="json"), config.project_version
        )
        st.download_button(
            tr("Export Settings"),
            data=json.dumps(preset_payload, ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
            file_name=SETTINGS_PRESET_FILE_NAME,
            mime="application/json",
            width="stretch",
            key="export_settings_preset_button",
            icon=":material/download:",
        )
        uploaded_preset = st.file_uploader(
            tr("Import Settings"),
            type=["json"],
            key="settings_preset_uploader",
        )
        if uploaded_preset is None:
            return
        # 上传的文件在之后每次 rerun 都会重新出现。记录已处理的文件标识，
        # 避免用户改完控件后被同一个预设反复覆盖。
        if st.session_state.get("settings_preset_file_id") == uploaded_preset.file_id:
            return

        st.session_state["settings_preset_file_id"] = uploaded_preset.file_id
        try:
            preset_params = _parse_settings_preset(uploaded_preset.getvalue())
        except Exception as e:
            logger.warning(f"failed to import settings preset: {e}")
            st.error(tr("Settings Preset Import Failed"))
            return

        st.session_state["settings_preset_payload"] = preset_params
        st.rerun()


def _render_key_backup_settings(panel):
    """渲染密钥备份的导出与恢复入口。"""
    with panel:
        backup_message = st.session_state.pop("key_backup_message", None)
        if backup_message:
            message_type, message = backup_message
            if message_type == "success":
                st.success(message)
            else:
                st.error(message)

        st.caption(tr("Key Backup Help"))
        st.warning(tr("Key Backup Warning"))

        backup_payload = _build_key_backup_payload(
            _RUNTIME_CONFIG_SECTIONS, config.project_version
        )
        backup_key_count = _count_backup_keys(backup_payload["keys"])
        st.caption(tr("Key Backup Summary").format(count=backup_key_count))
        st.download_button(
            tr("Export Keys"),
            data=json.dumps(backup_payload, ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
            file_name=KEY_BACKUP_FILE_NAME,
            mime="application/json",
            disabled=backup_key_count == 0,
            width="stretch",
            key="export_key_backup_button",
            icon=":material/download:",
        )

        uploaded_backup = st.file_uploader(
            tr("Import Keys"),
            type=["json"],
            key="key_backup_uploader",
        )
        if uploaded_backup is None:
            return
        if st.session_state.get("key_backup_file_id") == uploaded_backup.file_id:
            return

        st.session_state["key_backup_file_id"] = uploaded_backup.file_id
        try:
            restored_keys = _parse_key_backup(
                uploaded_backup.getvalue(), _RUNTIME_CONFIG_SECTIONS
            )
        except Exception as e:
            logger.warning(f"failed to import key backup: {e}")
            st.session_state["key_backup_message"] = (
                "error",
                tr("Key Restore Failed"),
            )
        else:
            restored_count = _apply_key_backup(restored_keys)
            _save_runtime_config()
            logger.info(f"restored keys from backup file: count={restored_count}")
            st.session_state["key_backup_message"] = (
                "success",
                tr("Keys Restored").format(count=restored_count),
            )
        # 主页面上的 TTS 密钥输入框也需要读取恢复后的配置，因此整页刷新。
        # 设置弹窗的打开状态保存在 session_state 中，刷新后会重新展开。
        st.rerun(scope="app")


# -----------------------------------------------------------------------------
# 设置与提示词弹窗
# -----------------------------------------------------------------------------


def _render_logging_settings():
    level, log_requests = get_logging_options()
    with st.expander(tr("Diagnostic Logging")):
        st.caption(tr("Diagnostic Logging Help"))
        with st.form("diagnostic_logging_form"):
            selected_level = st.selectbox(
                tr("Log Level"), LOG_LEVELS, index=LOG_LEVELS.index(level),
                key="diagnostic_log_level",
            )
            selected_trace = st.checkbox(
                tr("Trace API Requests"), value=log_requests,
                key="diagnostic_log_requests",
                help=tr("Trace API Requests Help"),
            )
            apply = st.form_submit_button(tr("Apply Logging Settings"))
        if apply:
            # 诊断不改变生成参数，直接生效；持久化仍走非阻塞配置队列。
            config.update_logging_options(selected_level, selected_trace)
            _save_runtime_config()
            st.success(tr("Logging Settings Applied"))


# 设置属于低频操作，使用中等尺寸 Dialog 避免长期占用主页面纵向空间，
# 同时控制阅读行宽，避免弹窗在宽屏设备上显得过于松散。
# Dialog 继承 fragment 行为，内部控件交互只重绘弹窗；函数末尾单独保存配置，
# 关闭时通过回调触发整页同步，确保生成流程读取最新 Provider 和界面设置。
@st.dialog(
    tr("Settings"),
    width="medium",
    on_dismiss=_dismiss_settings_dialog,
)
def _render_settings_dialog():
    with st.container():
        # 历史 hide_config 只用于隐藏旧基础设置面板。改为固定设置入口后，该值
        # 不再有用户可见意义，统一迁移为 false，避免旧配置影响后续版本。
        _set_runtime_config("app", "hide_config", False)
        settings_tab_labels = [
            tr("LLM Settings Tab"),
            tr("Material API Tab"),
            tr("Auto-Publish Settings"),
            tr("Interface Settings Tab"),
            tr("Key Backup Tab"),
            tr("Cache Management Tab"),
        ]
        settings_tab_targets = {
            "llm": tr("LLM Settings Tab"),
            "material": tr("Material API Tab"),
        }
        settings_tabs_key = localized_widget_key("settings_dialog_tabs")
        target_tab = st.session_state.pop("settings_dialog_target_tab", None)
        if target_tab in settings_tab_targets:
            # st.tabs 使用显示 label 作为状态值。入口按钮只保存稳定业务 ID，
            # 到这里再写入当前语言的 label，即可精确定位且兼容语言切换。
            st.session_state[settings_tabs_key] = settings_tab_targets[target_tab]

        (
            middle_config_panel,
            right_config_panel,
            publish_config_panel,
            left_config_panel,
            key_backup_panel,
            cache_config_panel,
        ) = st.tabs(
            settings_tab_labels,
            key=settings_tabs_key,
            on_change="rerun",
        )

        with publish_config_panel:
            st.write(tr("Automatically publish generated videos to social media using upload-post.com"))
            st.info(
                tr("Upload-Post Setup Guide").format(
                    api_keys_url=UPLOAD_POST_API_KEYS_URL,
                    manage_users_url=UPLOAD_POST_MANAGE_USERS_URL,
                )
            )

            is_enabled = config.app.get("upload_post_enabled", False)
            is_auto = config.app.get("upload_post_auto_upload", False)

            # 两个键各自独立:enabled 允许外部流程调用 Upload-Post,
            # auto_upload 才决定渲染完成后是否自动发布。合并成一个复选框会在
            # 两键不一致的配置下,仅打开设置对话框就把 enabled 改写为 False。
            upload_post_enabled = st.checkbox(
                tr("Enable Upload-Post Integration"),
                value=is_enabled,
                key="upload_post_enabled_checkbox"
            )
            if upload_post_enabled != is_enabled:
                _set_runtime_config("app", "upload_post_enabled", upload_post_enabled)

            upload_post_auto_upload = st.checkbox(
                tr("Enable Auto-Publish"),
                value=is_auto,
                key="upload_post_auto_upload_checkbox"
            )
            if upload_post_auto_upload != is_auto:
                _set_runtime_config("app", "upload_post_auto_upload", upload_post_auto_upload)

            upload_post_api_key = st.text_input(
                tr("Upload-Post API Key"),
                value=config.app.get("upload_post_api_key", ""),
                type="password",
                help=tr("Upload-Post API Key Help").format(
                    api_keys_url=UPLOAD_POST_API_KEYS_URL
                ),
                key="upload_post_api_key_input"
            )
            if upload_post_api_key != config.app.get("upload_post_api_key", ""):
                _set_runtime_config("app", "upload_post_api_key", upload_post_api_key)

            upload_post_username = st.text_input(
                tr("Upload-Post Profile Username"),
                value=config.app.get("upload_post_username", ""),
                help=tr("Upload-Post Profile Username Help").format(
                    manage_users_url=UPLOAD_POST_MANAGE_USERS_URL
                ),
                key="upload_post_username_input"
            )
            if upload_post_username != config.app.get("upload_post_username", ""):
                _set_runtime_config("app", "upload_post_username", upload_post_username)

            upload_post_platforms = st.multiselect(
                tr("Platforms"),
                options=["tiktok", "instagram", "youtube"],
                default=config.app.get("upload_post_platforms", ["tiktok", "instagram"]),
                help="Select platforms to publish to",
                key="upload_post_platforms_multiselect"
            )
            if upload_post_platforms != config.app.get("upload_post_platforms", ["tiktok", "instagram"]):
                _set_runtime_config("app", "upload_post_platforms", upload_post_platforms)

            if "youtube" in upload_post_platforms:
                yt_status_options = ["public", "private", "unlisted"]
                yt_saved = config.app.get("upload_post_youtube_privacy_status", "public")
                if yt_saved not in yt_status_options:
                    yt_saved = "public"
                upload_post_youtube_privacy_status = st.selectbox(
                    tr("YouTube Privacy Status"),
                    options=yt_status_options,
                    index=yt_status_options.index(yt_saved),
                    key="upload_post_youtube_privacy_status_selectbox"
                )
                if upload_post_youtube_privacy_status != config.app.get("upload_post_youtube_privacy_status", "public"):
                    _set_runtime_config("app", "upload_post_youtube_privacy_status", upload_post_youtube_privacy_status)

                # 受众声明只影响 YouTube 发布，不改变生成内容或其它平台的请求。
                # 使用真正的布尔选项，避免把展示文字或字符串当成 API 参数。
                saved_audience = config.app.get("upload_post_youtube_made_for_kids", False)
                audience_labels = {False: tr("Not Made for Kids"), True: tr("Made for Kids")}
                made_for_kids = st.selectbox(
                    tr("YouTube Audience"),
                    options=[False, True],
                    # 非法配置保持未选择，不在打开设置时擅自改成非儿童声明。
                    index=int(saved_audience) if isinstance(saved_audience, bool) else None,
                    format_func=audience_labels.get,
                    help=tr("YouTube Audience Help"),
                    key="upload_post_youtube_made_for_kids_selectbox",
                )
                if isinstance(made_for_kids, bool):
                    _set_runtime_config("app", "upload_post_youtube_made_for_kids", made_for_kids)

        # 左侧面板 - 日志设置
        with left_config_panel:
            hide_log = st.checkbox(
                tr("Hide Log"),
                value=config.ui.get("hide_log", False),
                key="hide_log_checkbox",
            )
            _set_runtime_config("ui", "hide_log", hide_log)
            _render_logging_settings()
            with st.expander("Factual Research"):
                research_providers = [
                    "ddgs", "gemini_search_grounding", "searxng", "brave_search",
                ]
                selected_research_provider = str(
                    config.app.get("fact_research_provider") or "ddgs"
                ).strip().lower()
                if selected_research_provider not in research_providers:
                    selected_research_provider = "ddgs"
                research_provider = st.selectbox(
                    "Research discovery provider",
                    options=research_providers,
                    index=research_providers.index(selected_research_provider),
                    key="fact_research_provider_select",
                    help=("DDGS is the no-key default. Gemini Search Grounding uses "
                          "gemini-2.5-flash and requires the Gemini project to be within "
                          "its free grounding quota; MPT cannot inspect the project's billing tier."),
                )
                _set_runtime_config("app", "fact_research_provider", research_provider)
                saved_fallback = str(config.app.get("fact_research_fallback_provider") or "").strip().lower()
                fallback_options = ["", "ddgs", "searxng", "brave_search"]
                if saved_fallback not in fallback_options:
                    saved_fallback = ""
                research_fallback = st.selectbox(
                    "Fallback discovery provider",
                    options=fallback_options,
                    format_func=lambda item: item or "None",
                    index=fallback_options.index(saved_fallback),
                    key="fact_research_fallback_provider_select",
                )
                _set_runtime_config("app", "fact_research_fallback_provider", research_fallback)
                searx_endpoint = st.text_input(
                    "SearXNG endpoint (optional; use an instance you are authorized to query)",
                    value=str(config.app.get("fact_research_searxng_url") or ""),
                    key="fact_research_searxng_endpoint_input",
                )
                _set_runtime_config("app", "fact_research_searxng_url", searx_endpoint.strip())
                health = source_research_service.research_health()
                status = health["provider_status"].get(health["provider"], "unknown")
                st.caption(f"Research health: {health['provider']} — {status}; checked online: no")
                if research_provider == "gemini_search_grounding":
                    st.warning("Gemini grounding has a published free-tier quota, but project eligibility and tier are not verified here. Select DDGS to guarantee no Gemini research requests.")

        _render_cache_management_settings(cache_config_panel)
        # 密钥恢复会写回配置并清除密码控件状态，必须在下面渲染这些控件之前执行。
        _render_key_backup_settings(key_backup_panel)

        # 中间面板 - LLM 设置

        with middle_config_panel:
            # 下拉顺序、默认 label 和稳定 provider id 全部来自 Registry；locale
            # 只覆盖展示文案，不再让 Main.py 维护第二份 Provider 列表。
            llm_provider_ids = [
                provider.provider_id for provider in LLM_PROVIDER_REGISTRY
            ]
            llm_provider_labels = {
                provider.provider_id: get_llm_provider_label(provider)
                for provider in LLM_PROVIDER_REGISTRY
            }
            saved_llm_provider = config.app.get(
                "llm_provider", DEFAULT_LLM_PROVIDER_ID
            ).lower()
            if saved_llm_provider not in llm_provider_ids:
                saved_llm_provider = DEFAULT_LLM_PROVIDER_ID

            llm_provider = stable_selectbox(
                tr("LLM Provider"),
                options=llm_provider_ids,
                default_value=saved_llm_provider,
                key="llm_provider_select",
                format_func=lambda provider_id: llm_provider_labels[provider_id],
            )
            # 配置表单和 Provider 说明并排展示，减少长说明在窄列中的换行，
            # 同时充分利用基础设置面板的横向空间。
            llm_form_panel, llm_help_panel = st.columns(
                [0.9, 1.1],
                gap="large",
                vertical_alignment="top",
            )
            llm_helper = llm_help_panel.container()
            _set_runtime_config("app", "llm_provider", llm_provider)
            llm_provider_spec = get_llm_provider(llm_provider)
            if llm_provider_spec is None:
                # 正常情况下下拉选项全部来自 Registry，不会进入该分支；保留
                # 明确错误用于诊断损坏的 session state 或后续接入遗漏。
                raise RuntimeError(f"unsupported llm provider: {llm_provider}")

            llm_api_key = config.app.get(llm_provider_spec.config_key("api_key"), "")
            configured_llm_base_url = config.app.get(
                llm_provider_spec.config_key("base_url"), ""
            )
            llm_default_base_url = llm_provider_spec.effective_default_base_url
            llm_base_url = configured_llm_base_url or llm_default_base_url
            llm_model_name = llm_provider_spec.resolve_model_name(
                config.app.get(llm_provider_spec.config_key("model_name"), "")
            )

            provider_tip_context = {}
            selected_service_endpoint = None
            if llm_provider_spec.service_endpoints:
                # Kimi 等 Provider 的中国站和国际站使用不同账号体系。只让用户
                # 选择服务区域，再由 Registry 同步 API 申请入口和 Base URL，
                # 避免手工组合错误。已有空 Base URL 配置继续沿用中国站，只有
                # 尚未填写 Key 的全新配置才根据界面语言推荐对应入口。
                selected_service_endpoint = (
                    llm_provider_spec.select_service_endpoint(
                        configured_llm_base_url,
                        has_api_key=bool(str(llm_api_key).strip()),
                        prefer_international=(
                            st.session_state.get("ui_language", "en") != "zh"
                        ),
                    )
                )
                endpoint_options = [
                    endpoint.endpoint_id
                    for endpoint in llm_provider_spec.service_endpoints
                ] + [CUSTOM_LLM_ENDPOINT_ID]
                default_endpoint_id = (
                    selected_service_endpoint.endpoint_id
                    if selected_service_endpoint
                    else CUSTOM_LLM_ENDPOINT_ID
                )
                endpoint_labels = {
                    endpoint.endpoint_id: (
                        tr_optional(
                            llm_provider_spec.endpoint_label_key(endpoint.endpoint_id),
                            fallback_language="en",
                        )
                        or endpoint.default_label
                    )
                    for endpoint in llm_provider_spec.service_endpoints
                }
                endpoint_labels[CUSTOM_LLM_ENDPOINT_ID] = (
                    tr_optional("Custom API Endpoint", fallback_language="en")
                    or "Custom API Endpoint"
                )
                with llm_form_panel:
                    selected_endpoint_id = stable_selectbox(
                        tr_optional(
                            llm_provider_spec.endpoint_selector_label_key,
                            fallback_language="en",
                        )
                        or tr("API Platform"),
                        options=endpoint_options,
                        default_value=default_endpoint_id,
                        key=f"{llm_provider}_service_endpoint_select",
                        format_func=lambda endpoint_id: endpoint_labels[endpoint_id],
                        help=(
                            tr_optional(
                                llm_provider_spec.endpoint_selector_help_key,
                                fallback_language="en",
                            )
                            or None
                        ),
                    )
                selected_service_endpoint = next(
                    (
                        endpoint
                        for endpoint in llm_provider_spec.service_endpoints
                        if endpoint.endpoint_id == selected_endpoint_id
                    ),
                    None,
                )
                if selected_service_endpoint:
                    llm_base_url = selected_service_endpoint.base_url
                    provider_tip_context.update(
                        {
                            "api_key_url": selected_service_endpoint.api_key_url,
                            "default_base_url": selected_service_endpoint.base_url,
                            "model_docs_url": selected_service_endpoint.model_docs_url,
                        }
                    )
                else:
                    # 自定义模式只保留用户明确保存的地址，不将某个标准区域伪装
                    # 成自定义值。输入为空时配置不会持久化，下一次仍回到兼容默认。
                    llm_base_url = str(configured_llm_base_url or "").strip()

            if llm_provider == "ollama":
                llm_default_base_url = config.get_default_ollama_base_url()
                if not llm_base_url:
                    llm_base_url = llm_default_base_url
                docker_hint = ""
                if config.is_running_in_container():
                    docker_hint = tr_optional(
                        "llm_provider_tips.ollama.docker_hint",
                        fallback_language="en",
                    )
                provider_tip_context["docker_hint"] = docker_hint

            tips = get_llm_provider_tips(llm_provider, **provider_tip_context)
            if tips:
                with llm_helper:
                    st.info(tips)

            st_llm_api_key = llm_api_key
            if llm_provider_spec.show_api_key:
                st_llm_api_key = llm_form_panel.text_input(
                    tr("API Key"),
                    value=llm_api_key,
                    type="password",
                    key=f"{llm_provider}_api_key_input",
                )

            st_llm_base_url = llm_base_url
            if llm_provider_spec.show_base_url:
                st_llm_base_url = llm_form_panel.text_input(
                    tr("Base Url"),
                    value=llm_base_url,
                    key=(
                        f"{llm_provider}_base_url_"
                        f"{selected_service_endpoint.endpoint_id}_input"
                        if selected_service_endpoint
                        else f"{llm_provider}_base_url_custom_input"
                    ),
                    disabled=selected_service_endpoint is not None,
                )
            st_llm_model_name = ""
            if llm_provider == "groq":
                effective_api_key = st_llm_api_key or llm_api_key
                effective_base_url = st_llm_base_url or llm_base_url
                groq_models = get_groq_model_ids(
                    api_key=effective_api_key,
                    base_url=effective_base_url,
                )

                if groq_models:
                    selected_index = 0
                    if llm_model_name in groq_models:
                        selected_index = groq_models.index(llm_model_name)

                    st_llm_model_name = llm_form_panel.selectbox(
                        tr("Model Name"),
                        options=groq_models,
                        index=selected_index,
                        key="groq_model_name_select",
                    )
                else:
                    st_llm_model_name = llm_form_panel.text_input(
                        tr("Model Name"),
                        value=llm_model_name,
                        key="groq_model_name_input",
                    )
                    if effective_api_key:
                        llm_form_panel.caption(tr("Groq Model List Load Failed"))
                    else:
                        llm_form_panel.caption(
                            tr("Groq API Key Required for Model List")
                        )
            else:
                st_llm_model_name = llm_form_panel.text_input(
                    tr("Model Name"),
                    value=llm_model_name,
                    key=f"{llm_provider}_model_name_input",
                )
            # 输入框展示 Registry 默认值，但配置只保存真实的用户覆盖值。
            # 这样默认模型、Base URL 更新后，未自定义的用户能够自动跟随。
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("api_key"),
                st_llm_api_key,
            )
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("base_url"),
                normalize_provider_override(
                    st_llm_base_url,
                    llm_default_base_url,
                ),
            )
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("model_name"),
                normalize_provider_override(
                    st_llm_model_name,
                    llm_provider_spec.default_model,
                ),
            )

            # Provider 专用字段也由 Registry 声明。例如 Cloudflare AI Gateway
            # 需要 Account ID；以后新增类似字段时无需再在 Main.py 增加判断。
            for field in llm_provider_spec.extra_fields:
                field_config_key = llm_provider_spec.config_key(field.config_suffix)
                field_value = llm_form_panel.text_input(
                    tr(field.label_key),
                    value=(config.app.get(field_config_key, "") or field.default_value),
                    type="password" if field.secret else "default",
                    key=f"{llm_provider}_{field.config_suffix}_input",
                )
                _set_runtime_config(
                    "app",
                    field_config_key,
                    normalize_provider_override(
                        field_value,
                        field.default_value,
                    ),
                )

            if llm_provider == "kimi_code" and middle_config_panel.open:
                from app.services import kimi_code_oauth
                from webui.kimi_login import render_kimi_login

                # 给嵌套 fragment 独立的容器，轮询重绘不占用后续控件的位置。
                with llm_form_panel.container():
                    render_kimi_login(
                        kimi_code_oauth.resolve_region(st_llm_base_url), tr
                    )

            if llm_form_panel.button(
                tr("Test LLM Connection"),
                key="test_llm_connection_button",
                width="stretch",
                type="secondary",
                icon=":material/network_check:",
            ):
                with config.try_runtime_config_lock() as lock_acquired:
                    if not lock_acquired:
                        llm_form_panel.warning(tr("Runtime Configuration Busy"))
                    else:
                        with llm_form_panel.spinner(tr("Testing LLM Connection")):
                            connection_ok, connection_error, connection_elapsed = (
                                llm.test_connection()
                            )

                if not lock_acquired:
                    connection_ok = None
                elif connection_ok:
                    llm_form_panel.success(
                        tr("LLM Connection Test Succeeded").format(
                            provider=llm_provider_labels[llm_provider],
                            model=st_llm_model_name or "-",
                            elapsed=f"{connection_elapsed:.2f}",
                        )
                    )
                else:
                    connection_error = format_llm_connection_error(
                        llm_provider,
                        st_llm_base_url,
                        connection_error,
                    )
                    llm_form_panel.error(
                        tr("LLM Connection Test Failed").format(error=connection_error)
                    )

        # 右侧面板 - API 密钥设置
        with right_config_panel:
            # 素材 Provider 按「搜索库存素材 / AI 生成视频 / AI 生成图片」
            # 分组，避免随着 Provider 增多后所有字段在一个长列表中混排。
            # 分组只调整展示层级，不改动已有配置键，旧用户升级后
            # 会继续读取原有 config.toml 值。
            with st.container(border=True):
                st.markdown(f"#### {tr('Stock Video APIs')}")
                st.caption(tr("Stock Video APIs Help"))

                pexels_api_key = _get_material_api_keys("pexels_api_keys")
                pixabay_api_key = _get_material_api_keys("pixabay_api_keys")
                coverr_api_key = _get_material_api_keys("coverr_api_keys")
                pexels_api_key = st.text_input(
                    tr("Pexels API Key"),
                    value=pexels_api_key,
                    type="password",
                    key="pexels_api_keys_input",
                )
                _save_material_api_keys("pexels_api_keys", pexels_api_key)

                pixabay_api_key = st.text_input(
                    tr("Pixabay API Key"),
                    value=pixabay_api_key,
                    type="password",
                    key="pixabay_api_keys_input",
                )
                _save_material_api_keys("pixabay_api_keys", pixabay_api_key)

                coverr_api_key = st.text_input(
                    tr("Coverr API Key"),
                    value=coverr_api_key,
                    type="password",
                    key="coverr_api_keys_input",
                )
                _save_material_api_keys("coverr_api_keys", coverr_api_key)

            with st.container(border=True):
                st.markdown(f"#### {tr('AI Video Generation APIs')}")
                st.caption(tr("AI Video Generation APIs Help"))

                # 视频生成 Provider 按赞助商优先展示，赞助商内部顺序
                # 与 VIDEO_SOURCE_GROUPS 一致：秘塔、OFox、胜算云、火山引擎。
                st.markdown(f"**{tr('Metaso MiniMax H3')}**")
                metaso_api_key = st.text_input(
                    tr("Metaso MiniMax API Key"),
                    value=str(
                        config.app.get("metaso_minimax_api_key", "") or ""
                    ).strip(),
                    type="password",
                    help=tr("Metaso MiniMax API Key Help"),
                    key="metaso_minimax_api_key_input",
                )
                _set_runtime_config(
                    "app", "metaso_minimax_api_key", metaso_api_key.strip()
                )
                configured_metaso_base_url = str(
                    config.app.get(
                        "metaso_minimax_base_url",
                        metaso_minimax.DEFAULT_BASE_URL,
                    )
                    or metaso_minimax.DEFAULT_BASE_URL
                ).strip()
                metaso_base_url = st.text_input(
                    tr("Metaso MiniMax Base URL"),
                    value=(
                        ""
                        if configured_metaso_base_url == metaso_minimax.DEFAULT_BASE_URL
                        else configured_metaso_base_url
                    ),
                    placeholder=metaso_minimax.DEFAULT_BASE_URL,
                    key="metaso_minimax_base_url_input",
                )
                _set_runtime_config(
                    "app",
                    "metaso_minimax_base_url",
                    metaso_base_url.strip() or metaso_minimax.DEFAULT_BASE_URL,
                )
                configured_metaso_resolution = (
                    str(
                        config.app.get(
                            "metaso_minimax_resolution",
                            metaso_minimax.DEFAULT_RESOLUTION,
                        )
                    )
                    .strip()
                    .upper()
                )
                metaso_resolution_options = sorted(
                    metaso_minimax.SUPPORTED_RESOLUTIONS,
                    key=lambda value: value != metaso_minimax.DEFAULT_RESOLUTION,
                )
                resolution_is_valid = (
                    configured_metaso_resolution
                    in metaso_minimax.SUPPORTED_RESOLUTIONS
                )
                if not resolution_is_valid:
                    # 分辨率直接影响计费。手工配置错误时保留原值并要求用户
                    # 主动选择，不能在打开设置弹窗时静默改成价格更高的 2K。
                    st.error(
                        tr("Metaso MiniMax Invalid Resolution").format(
                            value=configured_metaso_resolution,
                            supported=", ".join(metaso_resolution_options),
                        )
                    )
                metaso_resolution = st.selectbox(
                    tr("Metaso MiniMax Resolution"),
                    options=metaso_resolution_options,
                    index=(
                        metaso_resolution_options.index(configured_metaso_resolution)
                        if resolution_is_valid
                        else None
                    ),
                    key="metaso_minimax_resolution_input",
                    help=tr("Metaso MiniMax Resolution Help"),
                    placeholder=tr("Select Metaso MiniMax Resolution"),
                )
                if metaso_resolution is not None:
                    _set_runtime_config(
                        "app", "metaso_minimax_resolution", metaso_resolution
                    )

                st.divider()
                st.markdown("**OfoxAI**")
                st.caption(f"[OfoxAI]({OFOX_REFERRAL_URL}) · {tr('OFox AI Video Help')}")
                ofox_api_key = st.text_input(
                    tr("OFox API Key"),
                    value=str(config.app.get("ofox_api_key", "") or ""),
                    type="password",
                    key="ofox_api_key_input",
                )
                _set_runtime_config("app", "ofox_api_key", ofox_api_key.strip())
                ofox_model = st.text_input(
                    tr("OFox Text-to-Video Model"),
                    value=str(
                        config.app.get(
                            "ofox_text_to_video_model",
                            ofox.DEFAULT_MODEL_ID,
                        )
                        or ofox.DEFAULT_MODEL_ID
                    ),
                    key="ofox_text_to_video_model_input",
                )
                _set_runtime_config(
                    "app", "ofox_text_to_video_model", ofox_model.strip()
                )
                configured_ofox_base_url = str(
                    config.app.get("ofox_base_url", ofox.DEFAULT_BASE_URL)
                    or ofox.DEFAULT_BASE_URL
                ).strip()
                ofox_base_url = st.text_input(
                    tr("OFox Base URL"),
                    value=(
                        ""
                        if configured_ofox_base_url == ofox.DEFAULT_BASE_URL
                        else configured_ofox_base_url
                    ),
                    placeholder=ofox.DEFAULT_BASE_URL,
                    key="ofox_base_url_input",
                )
                _set_runtime_config(
                    "app",
                    "ofox_base_url",
                    ofox_base_url.strip() or ofox.DEFAULT_BASE_URL,
                )
                ofox_vendor_options = [
                    (tr("OFox Vendor BytePlus"), "byteplus"),
                    (tr("OFox Vendor Volcengine"), "volcengine"),
                    (tr("OFox Vendor Auto"), ""),
                ]
                configured_ofox_vendor = str(
                    config.app.get("ofox_provider", ofox.DEFAULT_PROVIDER_TYPE)
                    or ""
                ).strip()
                if configured_ofox_vendor not in {
                    value for _, value in ofox_vendor_options
                }:
                    # 用户在 config.toml 手工钉定了其它厂商名时保留该选择，
                    # 避免打开设置页就被下拉框覆盖回默认值。
                    ofox_vendor_options.append(
                        (configured_ofox_vendor, configured_ofox_vendor)
                    )
                selected_ofox_vendor = stable_selectbox(
                    tr("OFox Upstream Vendor"),
                    options=[value for _, value in ofox_vendor_options],
                    default_value=configured_ofox_vendor,
                    key="ofox_provider_select",
                    format_func=lambda value: dict(
                        (v, label) for label, v in ofox_vendor_options
                    )[value],
                    help=tr("OFox Upstream Vendor Help"),
                )
                _set_runtime_config("app", "ofox_provider", selected_ofox_vendor)

                st.divider()
                st.markdown(f"**{tr('Shengsuan Cloud AI Video')}**")
                app_config_snapshot = config.snapshot_config_with_pending(config.app)
                if (
                    str(app_config_snapshot.get("llm_provider", "") or "").lower()
                    == "shengsuanyun"
                ):
                    # 大模型 Provider 已选胜算云时，视频生成直接复用
                    # 同一密钥，不再展示一个容易引起歧义的独立输入框。
                    st.caption(tr("Shengsuan Cloud API Key Reused"))
                else:
                    configured_loomloom_token = str(
                        app_config_snapshot.get("loomloom_api_token", "") or ""
                    ).strip()
                    loomloom_api_token = st.text_input(
                        tr("Shengsuan Cloud API Key"),
                        value=configured_loomloom_token,
                        type="password",
                        key="loomloom_api_token_input",
                        help=tr("Shengsuan Cloud API Key Help"),
                        placeholder=tr("Shengsuan Cloud API Key Placeholder"),
                    ).strip()
                    _set_runtime_config(
                        "app", "loomloom_api_token", loomloom_api_token
                    )

                st.divider()
                seedance_api_key_value = str(
                    config.app.get("volcengine_seedance_api_key", "") or ""
                ).strip()
                shared_ark_api_key = str(
                    config.app.get("volcengine_api_key", "") or ""
                ).strip()
                environment_ark_api_key = os.getenv(
                    "VOLCENGINE_ARK_API_KEY", ""
                ).strip()
                seedance_reuses_llm_key = bool(
                    not seedance_api_key_value
                    and not environment_ark_api_key
                    and shared_ark_api_key
                )
                seedance_title = f"**{tr('Volcano Engine Seedance')}**"
                if seedance_reuses_llm_key:
                    # 只有复用大模型密钥无法从当前输入框直接看出，保留该提示
                    # 可以避免用户误以为必须重复填写；普通配置状态不再赘述。
                    seedance_title += f" :blue[{tr('Reusing LLM API Key')}]"
                st.markdown(seedance_title)
                seedance_api_key = st.text_input(
                    tr("Volcano Engine Ark API Key"),
                    value=seedance_api_key_value,
                    type="password",
                    help=tr("Volcano Engine Ark API Key Help"),
                    key="volcengine_seedance_api_key_input",
                )
                _set_runtime_config(
                    "app", "volcengine_seedance_api_key", seedance_api_key.strip()
                )
                configured_seedance_model = str(
                    config.app.get(
                        "volcengine_seedance_model",
                        volcengine_seedance.DEFAULT_MODEL_ID,
                    )
                    or volcengine_seedance.DEFAULT_MODEL_ID
                ).strip()
                seedance_model = st.text_input(
                    tr("Volcano Engine Seedance Model"),
                    # 内置默认值通过 placeholder 展示，用户自定义的
                    # 模型或接入点 ID 仍作为真实值展示和保存。
                    value=(
                        ""
                        if configured_seedance_model
                        == volcengine_seedance.DEFAULT_MODEL_ID
                        else configured_seedance_model
                    ),
                    placeholder=volcengine_seedance.DEFAULT_MODEL_ID,
                    key="volcengine_seedance_model_input",
                )
                _set_runtime_config(
                    "app",
                    "volcengine_seedance_model",
                    seedance_model.strip() or volcengine_seedance.DEFAULT_MODEL_ID,
                )
                configured_seedance_base_url = str(
                    config.app.get(
                        "volcengine_seedance_base_url",
                        volcengine_seedance.DEFAULT_BASE_URL,
                    )
                    or volcengine_seedance.DEFAULT_BASE_URL
                ).strip()
                seedance_base_url = st.text_input(
                    tr("Volcano Engine Ark Base URL"),
                    value=(
                        ""
                        if configured_seedance_base_url
                        == volcengine_seedance.DEFAULT_BASE_URL
                        else configured_seedance_base_url
                    ),
                    placeholder=volcengine_seedance.DEFAULT_BASE_URL,
                    key="volcengine_seedance_base_url_input",
                )
                _set_runtime_config(
                    "app",
                    "volcengine_seedance_base_url",
                    seedance_base_url.strip() or volcengine_seedance.DEFAULT_BASE_URL,
                )

                st.divider()
                wavespeed_api_key = _get_material_api_keys("wavespeed_api_keys")
                st.markdown("**WaveSpeed**")
                wavespeed_api_key = st.text_input(
                    tr("WaveSpeed API Key"),
                    value=wavespeed_api_key,
                    type="password",
                    key="wavespeed_api_keys_input",
                )
                _save_material_api_keys("wavespeed_api_keys", wavespeed_api_key)

                st.divider()
                st.markdown(f"**{tr('MuAPI AI Video')}**")
                st.caption(tr("MuAPI AI Video Help"))
                muapi_api_key = st.text_input(
                    tr("MuAPI API Key"),
                    value=str(config.app.get("muapi_api_key", "") or ""),
                    type="password",
                    help=tr("MuAPI API Key Help"),
                    key="muapi_api_key_input",
                )
                _set_runtime_config("app", "muapi_api_key", muapi_api_key.strip())
                configured_muapi_base_url = str(
                    config.app.get("muapi_base_url", muapi.DEFAULT_BASE_URL)
                    or muapi.DEFAULT_BASE_URL
                ).strip()
                muapi_base_url = st.text_input(
                    tr("MuAPI Base URL"),
                    value=(
                        ""
                        if configured_muapi_base_url == muapi.DEFAULT_BASE_URL
                        else configured_muapi_base_url
                    ),
                    placeholder=muapi.DEFAULT_BASE_URL,
                    key="muapi_base_url_input",
                    help=tr("MuAPI Base URL Help"),
                )
                _set_runtime_config(
                    "app",
                    "muapi_base_url",
                    muapi_base_url.strip() or muapi.DEFAULT_BASE_URL,
                )
                configured_muapi_endpoint = str(
                    config.app.get("muapi_video_endpoint", muapi.DEFAULT_ENDPOINT)
                    or muapi.DEFAULT_ENDPOINT
                ).strip()
                muapi_endpoint = st.text_input(
                    tr("MuAPI Video Endpoint"),
                    value=(
                        ""
                        if configured_muapi_endpoint == muapi.DEFAULT_ENDPOINT
                        else configured_muapi_endpoint
                    ),
                    placeholder=muapi.DEFAULT_ENDPOINT,
                    key="muapi_video_endpoint_input",
                    help=tr("MuAPI Video Endpoint Help"),
                )
                _set_runtime_config(
                    "app",
                    "muapi_video_endpoint",
                    muapi_endpoint.strip() or muapi.DEFAULT_ENDPOINT,
                )
                configured_muapi_resolution = str(
                    config.app.get("muapi_resolution", muapi.DEFAULT_RESOLUTION)
                    or muapi.DEFAULT_RESOLUTION
                ).strip()
                muapi_resolution = st.text_input(
                    tr("MuAPI Resolution"),
                    value=(
                        ""
                        if configured_muapi_resolution == muapi.DEFAULT_RESOLUTION
                        else configured_muapi_resolution
                    ),
                    placeholder=muapi.DEFAULT_RESOLUTION,
                    key="muapi_resolution_input",
                    help=tr("MuAPI Resolution Help"),
                )
                _set_runtime_config(
                    "app",
                    "muapi_resolution",
                    muapi_resolution.strip() or muapi.DEFAULT_RESOLUTION,
                )


            with st.container(border=True):
                st.markdown(f"#### {tr('AI Image Generation APIs')}")
                st.caption(tr("AI Image Generation APIs Help"))
                st.markdown(f"**{tr('OpenAI Compatible Text-to-Image')}**")

                openai_image_base_url = st.text_input(
                    tr("OpenAI Image Base URL"),
                    value=str(config.app.get("openai_image_base_url", "") or ""),
                    placeholder="https://api.openai.com/v1",
                    key="openai_image_base_url_input",
                )
                _set_runtime_config(
                    "app", "openai_image_base_url", openai_image_base_url.strip()
                )

                openai_image_api_key = _get_material_api_keys(
                    "openai_image_api_keys"
                )
                openai_image_api_key = st.text_input(
                    tr("OpenAI Image API Key"),
                    value=openai_image_api_key,
                    type="password",
                    help=tr("OpenAI Image API Key Help"),
                    key="openai_image_api_keys_input",
                )
                _save_material_api_keys(
                    "openai_image_api_keys", openai_image_api_key
                )

                openai_image_model = st.text_input(
                    tr("OpenAI Image Model"),
                    value=str(config.app.get("openai_image_model", "") or ""),
                    placeholder="gpt-image-2",
                    key="openai_image_model_input",
                )
                _set_runtime_config(
                    "app", "openai_image_model", openai_image_model.strip()
                )
                # 只展示参考值，不将 OpenAI 官方端点写成默认配置。
                # 兼容服务的 Base URL 和模型 ID 没有统一值；留空不会让
                # 用户在未知情时误连官方付费接口，也不会覆盖旧配置。
                st.caption(tr("OpenAI Image Configuration Example"))

                with st.expander(
                    tr("OpenAI Image Advanced Settings"), expanded=False
                ):
                    openai_image_size = st.text_input(
                        tr("OpenAI Image Size"),
                        value=str(config.app.get("openai_image_size", "") or ""),
                        placeholder="1024x1536",
                        help=tr("OpenAI Image Size Help"),
                        key="openai_image_size_input",
                    )
                    _set_runtime_config(
                        "app", "openai_image_size", openai_image_size.strip()
                    )

                    openai_image_prompt_template = st.text_input(
                        tr("OpenAI Image Prompt Template"),
                        value=str(
                            config.app.get("openai_image_prompt_template", "") or ""
                        ),
                        placeholder="cinematic photo of {term}, photorealistic",
                        help=tr("OpenAI Image Prompt Template Help"),
                        key="openai_image_prompt_template_input",
                    )
                    _set_runtime_config(
                        "app",
                        "openai_image_prompt_template",
                        openai_image_prompt_template.strip(),
                    )

    _save_runtime_config()


# -----------------------------------------------------------------------------
# 主生成表单：文案、视频、音频与字幕面板
# -----------------------------------------------------------------------------


def _create_loomloom_script_backend():
    """从当前 WebUI/config.toml 配置创建批量文案客户端。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    settings = loomloom.LoomLoomSettings.from_mapping(app_config_snapshot)
    return loomloom.LoomLoomScriptBackend(settings)


def _create_loomloom_video_backend():
    """使用项目默认 SkillBot 和当前有效凭证创建视频客户端。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    settings = loomloom.video_settings_from_mapping(app_config_snapshot)
    return loomloom.LoomLoomVideoBackend(settings)


def _effective_loomloom_api_token():
    """读取 WebUI 尚未落盘或 config.toml 中的胜算云 API Key。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    return loomloom.resolve_api_token(app_config_snapshot)


def _effective_script_generation_backend():
    """读取包含 WebUI 待保存修改的文案生成方式。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    backend = str(
        app_config_snapshot.get("script_generation_backend", "local") or "local"
    ).strip()
    return backend if backend in {"local", "loomloom"} else "local"




def _script_generation_method_help(selected_backend):
    """让“文案生成方式”的问号内容严格跟随当前选择。"""
    if selected_backend != "loomloom":
        return tr("Script Generation Method Help")

    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    guidance = [tr("LoomLoom Batch Script Generation Help")]
    if (
        str(app_config_snapshot.get("llm_provider", "") or "").strip().lower()
        == "shengsuanyun"
    ):
        guidance.append(tr("Shengsuan Cloud API Key Reused"))
    guidance.append(tr("Shengsuan Cloud API Key Link"))
    return "\n\n".join(guidance)


def _loomloom_video_scene_prompts(video_terms, subject, scene_count):
    """按素材关键词生成有限数量的场景描述，供视频模型逐段生成素材。"""
    if isinstance(video_terms, str):
        terms = [
            term.strip() for term in re.split(r"[,，\n]", video_terms) if term.strip()
        ]
    elif isinstance(video_terms, list):
        terms = [
            str(term or "").strip() for term in video_terms if str(term or "").strip()
        ]
    else:
        terms = []
    fallback = str(subject or "").strip()
    if not terms and fallback:
        terms = [fallback]
    if not terms:
        return ()
    return tuple(
        (
            terms[index % len(terms)]
            if index < len(terms)
            else f"{terms[index % len(terms)]}; alternative camera angle {index + 1}"
        )
        for index in range(int(scene_count))
    )


def _loomloom_video_signature(batch, credential_fingerprint):
    """将全部计费输入和凭证摘要纳入签名，参数变化后强制重新报价。"""
    payload = {
        "inputRows": [dict(row) for row in batch.input_rows],
        "credentialFingerprint": str(credential_fingerprint or "").strip(),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _loomloom_video_account_signature(token):
    """服务地址和凭据共同隔离模型目录及报价，不能跨端点复用已确认状态。"""
    values = config.snapshot_config_with_pending(config.app)
    base_url = str(values.get("loomloom_base_url") or loomloom.DEFAULT_BASE_URL).strip().rstrip("/")
    payload = json.dumps([base_url, str(token or "").strip()])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_loomloom_video_capability(token, *, force=False):
    """按当前凭证缓存 Profile；刷新失败时保留同一凭证最近的成功结果。"""
    normalized_token = str(token or "").strip()
    if not normalized_token:
        return None

    fingerprint = _loomloom_video_account_signature(normalized_token)
    if st.session_state.get("loomloom_video_capability_fingerprint") != fingerprint:
        st.session_state["loomloom_video_capability"] = None
        st.session_state["loomloom_video_capability_fingerprint"] = fingerprint
        st.session_state["loomloom_video_capability_load_attempt"] = ""
        st.session_state["loomloom_video_capability_error"] = ""

    should_load = force or (
        st.session_state.get("loomloom_video_capability_load_attempt") != fingerprint
    )
    if should_load:
        st.session_state["loomloom_video_capability_load_attempt"] = fingerprint
        try:
            capability = _create_loomloom_video_backend().resolve_video_capability()
        except (loomloom.LoomLoomError, ValueError) as exc:
            logger.warning(
                f"failed to load LoomLoom video capability: error={type(exc).__name__}"
            )
            st.session_state["loomloom_video_capability_error"] = str(exc)
        else:
            st.session_state["loomloom_video_capability"] = capability
            st.session_state["loomloom_video_capability_error"] = ""

    capability = st.session_state.get("loomloom_video_capability")
    return (
        capability if isinstance(capability, loomloom.LoomLoomVideoCapability) else None
    )


def _normalize_loomloom_model_identifier(value):
    """统一展示名和模型 ID 的分隔符、大小写，供本地价格表安全匹配。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _loomloom_video_model_price(model):
    """返回已知模型的（下拉框短价、选中后完整参考价）；未知模型返回空值。"""
    identifiers = {
        _normalize_loomloom_model_identifier(model.model_id),
        _normalize_loomloom_model_identifier(model.display_name),
    }
    for aliases, compact_price, detailed_price in LOOMLOOM_VIDEO_MODEL_PRICES:
        if identifiers.intersection(aliases):
            return compact_price, detailed_price
    return "", ""


def _format_loomloom_video_model_option(model):
    """在模型名右侧展示短价格，避免多档分辨率价格把下拉框撑得过宽。"""
    compact_price, _ = _loomloom_video_model_price(model)
    return f"{model.display_name} · {compact_price}" if compact_price else model.display_name


def _effective_voice_rate_before_audio_panel():
    """视频面板位于音频面板之前，需从现有控件状态或配置读取当前语速。"""
    raw_rate = st.session_state.get(
        localized_widget_key("voice_rate_select"),
        config.ui.get("voice_rate", 1.0),
    )
    try:
        rate = float(raw_rate)
    except (TypeError, ValueError, OverflowError):
        return 1.0
    return rate if math.isfinite(rate) and rate > 0 else 1.0


def _matching_full_voice_preview_duration(script, voice_rate):
    """仅在文案、Provider、音色和语速均未变化时采用完整试听的真实时长。"""
    cached = st.session_state.get("voice_preview_audio")
    if not isinstance(cached, dict) or cached.get("preview_type") != "full":
        return None
    spoken_script = hindi_script.build_representations(
        str(script or ""), st.session_state.get(localized_widget_key("script_language_select"), "hi"),
        pronunciation_overrides=st.session_state.get("pronunciation_overrides", {}),
    ).spoken_script
    script_digest = hashlib.sha256(spoken_script.encode("utf-8")).hexdigest()
    if cached.get("content_digest") != script_digest:
        return None

    current_tts_server = st.session_state.get(
        localized_widget_key("tts_server_select"),
        config.ui.get("tts_server", "azure-tts-v1"),
    )
    current_voice_name = st.session_state.get(
        localized_widget_key(f"speech_synthesis_select_{current_tts_server}"),
        config.ui.get("voice_name", ""),
    )
    try:
        cached_voice_rate = float(cached.get("voice_rate", 0))
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        cached.get("tts_server") != current_tts_server
        or cached.get("voice_name") != current_voice_name
        or not math.isfinite(cached_voice_rate)
        or not math.isclose(cached_voice_rate, voice_rate)
    ):
        return None

    narrator_values = {
        "Standard": "standard",
        "Desi Savage Hindi": "desi_savage_aggressive",
    }
    profanity_values = {
        "Mild": "mild",
        "Savage — Monetization Aware": "savage_monetization_aware",
        "Savage Unfiltered": "savage_unfiltered",
    }
    settings = generation_settings_service.resolve_generation_settings(
        task_override={
            "visual_style": VISUAL_STYLE_VALUES.get(
                st.session_state.get("visual_style_select"), "auto"
            ),
            "narrator_preset": narrator_values.get(
                st.session_state.get("narrator_preset_select")
            ),
            "profanity_level": profanity_values.get(
                st.session_state.get("profanity_level_select")
            ),
            "tts_provider": current_tts_server,
            "tts_model": config.ui.get("gemini_tts_model", "gemini-3.8-flash-tts"),
            "voice_name": current_voice_name,
            "voice_rate": voice_rate,
            "voice_volume": config.ui.get("voice_volume", 1.0),
        }
    )
    plan = tm.emotion_storyboard.build(
        spoken_script, narrator_preset=settings.narrator_preset,
        profanity_level=settings.profanity_level,
    )
    identity = generation_settings_service.synthesis_fingerprint(spoken_script, settings, plan)
    if cached.get("synthesis_fingerprint") != identity:
        return None

    duration = cached.get("duration")
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        return None
    return float(duration)


def _loomloom_video_coverage_plan(params):
    """按真实或估算旁白时长推荐素材数；不足部分仍由原有循环逻辑补齐。"""
    script = str(params.video_script or "").strip()
    if not script:
        return None

    voice_rate = _effective_voice_rate_before_audio_panel()
    actual_duration = _matching_full_voice_preview_duration(script, voice_rate)
    if actual_duration is not None:
        duration_min = duration_max = actual_duration
        basis_key = "AI Video Duration Basis Actual"
    else:
        estimated = _estimate_voiceover_duration_range(script, voice_rate)
        if not estimated:
            return None
        duration_min, duration_max = estimated
        basis_key = "AI Video Duration Basis Estimated"

    clip_duration = max(float(params.video_clip_duration or 1), 1.0)
    needed_min = max(math.ceil(duration_min / clip_duration), 1)
    needed_max = max(math.ceil(duration_max / clip_duration), needed_min)
    return {
        "script_digest": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "basis_key": basis_key,
        "duration_min": float(duration_min),
        "duration_max": float(duration_max),
        "clip_duration": clip_duration,
        "needed_min": needed_min,
        "needed_max": needed_max,
        # 推荐值优先覆盖保守上界，但绝不突破服务端允许的付费任务上限。
        "recommended_count": min(needed_max, loomloom.MAX_VIDEO_SCENES),
    }


def _format_numeric_range(minimum, maximum, digits=1):
    if math.isclose(float(minimum), float(maximum)):
        return f"{float(maximum):.{digits}f}"
    return f"{float(minimum):.{digits}f}–{float(maximum):.{digits}f}"


def _selected_loomloom_video_model(capability):
    """返回仍在当前 Profile 候选中的用户选择，不做静默回退。"""
    selected_model_id = str(
        st.session_state.get("loomloom_video_model_id", "") or ""
    ).strip()
    eligible_model_ids = {model.model_id for model in capability.models}
    return selected_model_id if selected_model_id in eligible_model_ids else ""


def _current_loomloom_video_quote_context(params):
    """根据当前页面参数构建默认 SkillBot 的视频报价批次。"""
    token = _effective_loomloom_api_token()
    fingerprint = _loomloom_video_account_signature(token) if token else ""
    capability = st.session_state.get("loomloom_video_capability")
    if (
        not isinstance(capability, loomloom.LoomLoomVideoCapability)
        or st.session_state.get("loomloom_video_capability_fingerprint") != fingerprint
    ):
        return None, ""
    model_id = _selected_loomloom_video_model(capability)
    scene_count = int(st.session_state.get("loomloom_video_scene_count", 1) or 1)
    prompts = _loomloom_video_scene_prompts(
        params.video_terms,
        params.video_subject or params.video_script,
        scene_count,
    )
    aspect_ratio = str(
        params.video_aspect.value
        if isinstance(params.video_aspect, VideoAspect)
        else params.video_aspect
    )
    if (
        not token
        or not model_id
        or not prompts
        or aspect_ratio not in capability.aspect_ratios
    ):
        return None, ""
    try:
        batch = _create_loomloom_video_backend().prepare_video_batch(
            subject=params.video_subject or params.video_script,
            scene_prompts=prompts,
            model_id=model_id,
            aspect_ratio=aspect_ratio,
        )
    except (loomloom.LoomLoomError, ValueError):
        return None, ""
    return batch, _loomloom_video_signature(batch, fingerprint)


def _retry_loomloom_video_quote():
    """用户主动重试时解除失败锁；不沿用之前的付费确认。"""
    st.session_state["loomloom_video_quote_error_signature"] = ""
    st.session_state["loomloom_video_quote_error"] = ""
    st.session_state["loomloom_video_confirm_charge"] = False


def _render_loomloom_video_settings(params):
    """渲染默认视频 SkillBot 的报价、报价失效和付费确认流程。"""
    st.caption(tr("Shengsuan Cloud AI Video Help"))
    if (
        str(
            config.snapshot_config_with_pending(config.app).get("llm_provider", "")
            or ""
        ).lower()
        == "shengsuanyun"
    ):
        st.caption(tr("Shengsuan Cloud API Key Reused"))

    token = _effective_loomloom_api_token()

    refresh_models = st.button(
        tr("Refresh AI Video Models"),
        key="loomloom_refresh_video_models",
        width="stretch",
        disabled=not token,
    )
    capability = _load_loomloom_video_capability(token, force=refresh_models)
    capability_error = str(
        st.session_state.get("loomloom_video_capability_error", "") or ""
    ).strip()
    if capability_error:
        st.warning(tr("AI Video Model List Load Failed").format(error=capability_error))

    if capability is not None:
        models_by_id = {model.model_id: model for model in capability.models}
        selected_model_id = str(
            st.session_state.get("loomloom_video_model_id", "") or ""
        ).strip()
        if not selected_model_id:
            selected_model_id = capability.default_model_id
            st.session_state["loomloom_video_model_id"] = selected_model_id

        model_options = list(models_by_id)
        if selected_model_id not in models_by_id:
            # 保留已失效的原选择，让用户明确看到状态并主动重选。直接把控件
            # 改成新的默认模型会让旧报价与用户认知不一致。
            model_options.insert(0, selected_model_id)

        selected_model_id = stable_selectbox(
            tr("AI Video Model"),
            options=model_options,
            default_value=selected_model_id,
            key="loomloom_video_model_select",
            format_func=lambda model_id: (
                _format_loomloom_video_model_option(models_by_id[model_id])
                if model_id in models_by_id
                else tr("Unavailable AI Video Model").format(model=model_id)
            ),
        )
        st.session_state["loomloom_video_model_id"] = selected_model_id
        if selected_model_id not in models_by_id:
            st.error(tr("Selected AI Video Model Unavailable"))
        else:
            _, detailed_price = _loomloom_video_model_price(
                models_by_id[selected_model_id]
            )
            if detailed_price:
                st.caption(
                    tr("AI Video Model Reference Price").format(price=detailed_price)
                )

        current_aspect_ratio = str(
            params.video_aspect.value
            if isinstance(params.video_aspect, VideoAspect)
            else params.video_aspect
        )
        if current_aspect_ratio not in capability.aspect_ratios:
            st.error(tr("Selected AI Video Ratio Unavailable"))

    coverage_plan = _loomloom_video_coverage_plan(params)
    pending_autofill_digest = str(
        st.session_state.get("loomloom_video_scene_autofill_digest", "") or ""
    )
    if (
        coverage_plan is not None
        and pending_autofill_digest == coverage_plan["script_digest"]
    ):
        # 只在“刚生成文案”或“刚取得完整试听真实时长”时推荐一次。
        # 消费标记后不再覆盖，用户随后手动调整段数会被完整保留。
        st.session_state["loomloom_video_scene_count"] = coverage_plan[
            "recommended_count"
        ]
        st.session_state["loomloom_video_scene_autofill_digest"] = ""

    scene_count = st.number_input(
        tr("AI Video Scene Count"),
        min_value=1,
        max_value=loomloom.MAX_VIDEO_SCENES,
        step=1,
        key="loomloom_video_scene_count",
    )
    _set_runtime_config("ui", "loomloom_video_scene_count", int(scene_count))
    if coverage_plan is not None:
        coverage_seconds = int(scene_count) * coverage_plan["clip_duration"]
        shortfall_min = max(
            coverage_plan["duration_min"] - coverage_seconds, 0.0
        )
        shortfall_max = max(
            coverage_plan["duration_max"] - coverage_seconds, 0.0
        )
        duration_basis = tr(coverage_plan["basis_key"]).format(
            duration=_format_numeric_range(
                coverage_plan["duration_min"], coverage_plan["duration_max"]
            )
        )
        coverage_message = tr("AI Video Material Coverage").format(
            basis=duration_basis,
            clip=_format_numeric_range(
                coverage_plan["clip_duration"], coverage_plan["clip_duration"]
            ),
            needed=_format_numeric_range(
                coverage_plan["needed_min"], coverage_plan["needed_max"], digits=0
            ),
            count=int(scene_count),
            coverage=_format_numeric_range(coverage_seconds, coverage_seconds),
            shortfall=_format_numeric_range(shortfall_min, shortfall_max),
        )
        if shortfall_max > 0:
            st.warning(coverage_message)
        else:
            st.caption(coverage_message)

    batch, input_signature = _current_loomloom_video_quote_context(params)
    if not token:
        st.warning(tr("Shengsuan Cloud API Key Required"))

    quote_result = st.session_state.get("loomloom_video_quote")
    quoted_batch = st.session_state.get("loomloom_video_batch")
    quote_is_current = bool(
        quote_result is not None
        and quoted_batch is not None
        and st.session_state.get("loomloom_video_input_signature") == input_signature
    )
    # 同一组参数失败后暂停自动请求，避免普通页面交互反复等待服务超时。
    # 签名包含账号、端点与全部计费输入；参数变化或用户主动重试后再询价。
    if st.session_state.get("loomloom_video_quote_error_signature") != input_signature:
        st.session_state["loomloom_video_quote_error_signature"] = ""
        st.session_state["loomloom_video_quote_error"] = ""
    quote_failed = bool(st.session_state.get("loomloom_video_quote_error"))
    # Quote 不创建付费任务，真正执行仍需用户明确勾选确认。
    if token and batch is not None and not quote_is_current and not quote_failed:
        st.session_state["loomloom_video_confirm_charge"] = False
        try:
            quote_result = _create_loomloom_video_backend().quote(batch)
        except (loomloom.LoomLoomError, ValueError) as exc:
            logger.warning(f"failed to quote LoomLoom videos: error={exc}")
            st.session_state["loomloom_video_quote_error_signature"] = input_signature
            st.session_state["loomloom_video_quote_error"] = str(exc) or type(exc).__name__
        else:
            st.session_state["loomloom_video_batch"] = batch
            st.session_state["loomloom_video_quote"] = quote_result
            st.session_state["loomloom_video_input_signature"] = input_signature
            st.session_state["loomloom_video_client_request_id"] = (
                f"mpt-video-{uuid4()}"
            )
            st.session_state["loomloom_video_confirm_charge"] = False
            logger.info(
                "LoomLoom video quote ready: "
                f"tasks={quote_result.task_count}, currency={quote_result.currency}, "
                f"estimated_payable_t={quote_result.estimated_buyer_payable_t}"
            )

    if st.session_state.get("loomloom_video_quote_error"):
        st.error(st.session_state["loomloom_video_quote_error"])
        st.button(
            tr("Retry AI Video Quote"),
            key="loomloom_retry_video_quote",
            on_click=_retry_loomloom_video_quote,
        )

    quote_result = st.session_state.get("loomloom_video_quote")
    quoted_batch = st.session_state.get("loomloom_video_batch")
    if quote_result is not None and quoted_batch is not None:
        display_amount = (
            quote_result.estimated_buyer_payable_amount
            or f"{quote_result.estimated_buyer_payable_t} T"
        )
        if quote_result.estimated_buyer_payable_t == 0:
            st.warning(tr("AI Video Quote Estimate Incomplete"))
        else:
            st.success(
                tr(
                    "AI Video Quote Summary Singular"
                    if quote_result.task_count == 1
                    else "AI Video Quote Summary"
                ).format(
                    tasks=quote_result.task_count,
                    amount=display_amount,
                    currency=quote_result.currency,
                )
            )
        quote_is_current = (
            st.session_state.get("loomloom_video_input_signature") == input_signature
        )
        if not quote_is_current:
            st.warning(tr("LoomLoom Quote Changed Warning"))
        st.checkbox(
            tr("Confirm AI Video Charge"),
            key="loomloom_video_confirm_charge",
            help=tr("Confirm AI Video Charge Help"),
            disabled=not quote_is_current,
        )


def _loomloom_script_signature(
    *,
    subject,
    language,
    candidate_count,
    duration_seconds,
    style,
    credential_fingerprint,
):
    payload = {
        "subject": str(subject or "").strip(),
        "language": str(language or "auto").strip() or "auto",
        "candidateCount": int(candidate_count),
        "durationSeconds": int(duration_seconds),
        "style": str(style or "").strip(),
        "credentialFingerprint": str(credential_fingerprint or "").strip(),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _render_local_script_generation(params):
    """保留 MoneyPrinterTurbo 原有的本地 LLM 脚本生成路径。"""
    if not st.button(
        tr("Generate Video Script and Keywords"),
        key="auto_generate_script",
        width="stretch",
        type="secondary",
        icon=":material/auto_awesome:",
    ):
        return

    if not params.video_subject:
        st.toast(tr("Please Enter the Video Subject First"))
        st.warning(tr("Please Enter the Video Subject First"))
        return

    with st.spinner(tr("Generating Video Script and Keywords")):

        def generate_script_and_terms(app_config_snapshot):
            script = llm.generate_script(
                video_subject=params.video_subject,
                language=params.video_language,
                paragraph_number=params.paragraph_number,
                video_script_prompt=params.video_script_prompt,
                custom_system_prompt=params.custom_system_prompt,
                app_config=app_config_snapshot,
                visual_style=params.visual_style,
                narrator_preset=params.narrator_preset,
                profanity_level=params.profanity_level or "savage_monetization_aware",
                savage_quality_policy=params.savage_quality_policy,
            )
            if script.startswith("Error: "):
                return script, ""
            terms = llm.generate_terms(
                params.video_subject,
                script,
                amount=(
                    max(1, len(tm.emotion_storyboard.build(
                        tm._represent_script(params, script).spoken_script,
                        narrator_preset=params.narrator_preset or "desi_savage_aggressive",
                        profanity_level=params.profanity_level or "savage_monetization_aware",
                    )))
                    if params.visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER
                    else 8 if params.match_materials_to_script else 5
                ),
                match_script_order=(
                    params.match_materials_to_script
                    or params.visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER
                ),
                app_config=app_config_snapshot,
                stock_video_style=params.stock_video_style,
                asmr_categories=params.asmr_categories,
            )
            return script, terms

        script, terms = _run_llm_read_operation(
            "generate_script_and_terms",
            generate_script_and_terms,
        )
        rejected = script_quality.parse_failed_draft(script)
        if rejected:
            draft, issues = rejected
            st.session_state.pop("video_script_quality_review", None)
            if draft:
                st.session_state["video_script"] = draft
            st.error(
                "Savage narration was rejected by the selected Strict quality policy. "
                "The actual draft is loaded in the script editor. "
                f"Issues: {'; '.join(issues)}"
            )
        elif "Error: " in script:
            st.error(tr(script))
        elif "Error: " in terms:
            st.error(tr(terms))
        else:
            st.session_state["video_script"] = script
            st.session_state["video_script_needs_style_review"] = False
            st.session_state["video_terms"] = ", ".join(terms)
            quality_warnings = (
                script_quality.savage_script_warnings(script)
                if params.visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER
                else []
            )
            if quality_warnings:
                st.session_state["video_script_quality_review"] = {
                    "digest": hashlib.sha256(script.strip().encode("utf-8")).hexdigest(),
                    "warnings": quality_warnings,
                }
            else:
                st.session_state.pop("video_script_quality_review", None)
            st.session_state["loomloom_video_scene_autofill_digest"] = (
                hashlib.sha256(script.strip().encode("utf-8")).hexdigest()
            )


def _render_loomloom_candidates():
    candidates = tuple(st.session_state.get("loomloom_script_candidates") or ())
    errors = tuple(st.session_state.get("loomloom_candidate_errors") or ())
    if errors:
        st.warning(
            tr("LoomLoom Candidate Errors").format(
                count=len(errors),
                details="; ".join(
                    f"#{error.row_index + 1}: {error.message}" for error in errors
                ),
            )
        )
    if not candidates:
        return

    selected_index = st.radio(
        tr("Choose Script Candidate"),
        options=list(range(len(candidates))),
        key="loomloom_selected_candidate",
        format_func=lambda index: (
            f"#{candidates[index].row_index + 1} {candidates[index].script[:80]}"
        ),
    )
    selected = candidates[selected_index]
    st.code(selected.script, language=None, wrap_lines=True)
    st.caption(", ".join(selected.video_terms))
    if st.button(
        tr("Use Selected Candidate"),
        key="loomloom_apply_candidate",
        type="primary",
        width="stretch",
    ):
        st.session_state["video_script"] = selected.script
        st.session_state["video_terms"] = ", ".join(selected.video_terms)
        # 与普通大模型生成文案保持一致：应用新候选后仅推荐一次素材数量。
        st.session_state["loomloom_video_scene_autofill_digest"] = (
            hashlib.sha256(selected.script.strip().encode("utf-8")).hexdigest()
        )
        st.toast(tr("LoomLoom Candidate Applied"))


def _handle_loomloom_poll_error(run_id, exc):
    """对脚本任务轮询错误做有限退避，确定性错误立即停止轮询。"""
    logger.warning(f"failed to poll LoomLoom run: run_id={run_id}, error={exc}")
    failure_count = int(st.session_state.get("loomloom_poll_failure_count", 0) or 0) + 1
    retryable = isinstance(exc, loomloom.LoomLoomAPIError) and exc.retryable
    if not retryable or failure_count >= LOOMLOOM_MAX_POLL_FAILURES:
        st.session_state["loomloom_run_error"] = str(exc)
        st.session_state["loomloom_poll_failure_count"] = 0
        st.session_state["loomloom_poll_retry_after"] = 0.0
        # 查询失败不等于远端付费任务失败。保留 run_id 并暂停自动轮询，让用户
        # 可以继续查询同一个任务；如果直接丢弃 ID 后重新提交，可能重复付费。
        st.session_state["loomloom_poll_paused"] = True
        st.rerun(scope="app")
        return

    retry_delay = min(2**failure_count, 30)
    st.session_state["loomloom_poll_failure_count"] = failure_count
    st.session_state["loomloom_poll_retry_after"] = time.monotonic() + retry_delay
    st.warning(
        tr("LoomLoom Poll Retry Warning").format(
            attempt=failure_count,
            max_attempts=LOOMLOOM_MAX_POLL_FAILURES,
        )
    )


@st.fragment(run_every="2s")
def _render_loomloom_run_progress():
    run_id = str(st.session_state.get("loomloom_run_id", "") or "").strip()
    if not run_id or st.session_state.get("loomloom_poll_paused", False):
        return
    retry_after = float(st.session_state.get("loomloom_poll_retry_after", 0.0) or 0.0)
    retry_wait_seconds = max(0, int(math.ceil(retry_after - time.monotonic())))
    if retry_wait_seconds > 0:
        st.info(
            tr("LoomLoom Poll Retry Pending").format(
                seconds=retry_wait_seconds,
            )
        )
        return
    try:
        backend = _create_loomloom_script_backend()
        run = backend.get_run(run_id)
    except loomloom.LoomLoomError as exc:
        _handle_loomloom_poll_error(run_id, exc)
        return

    st.session_state["loomloom_run_status"] = run.status
    if run.status == "completed":
        try:
            result = backend.get_script_results(run_id)
        except loomloom.LoomLoomError as exc:
            _handle_loomloom_poll_error(run_id, exc)
            return
        st.session_state["loomloom_poll_failure_count"] = 0
        st.session_state["loomloom_poll_retry_after"] = 0.0
        st.session_state["loomloom_poll_paused"] = False
        st.session_state["loomloom_script_candidates"] = result.candidates
        st.session_state["loomloom_candidate_errors"] = result.errors
        st.session_state["loomloom_selected_candidate"] = 0
        st.session_state["loomloom_run_id"] = ""
        st.rerun(scope="app")
        return
    if run.status in {"failed", "cancelled", "canceled"}:
        st.session_state["loomloom_run_error"] = run.first_error_message or run.status
        st.session_state["loomloom_run_id"] = ""
        st.session_state["loomloom_poll_paused"] = False
        st.rerun(scope="app")
        return

    st.session_state["loomloom_poll_failure_count"] = 0
    st.session_state["loomloom_poll_retry_after"] = 0.0
    st.info(
        tr("LoomLoom Run Progress").format(
            completed=run.completed_tasks,
            total=run.total_tasks,
        )
    )


def _render_loomloom_script_generation(params):
    st.caption(tr("LoomLoom Batch Script Generation Help"))
    effective_token = _effective_loomloom_api_token()
    if not effective_token:
        st.warning(tr("Shengsuan Cloud API Key Required"))

    candidate_col, duration_col = st.columns(2)
    candidate_count = candidate_col.number_input(
        tr("Script Candidate Count"),
        min_value=1,
        max_value=loomloom.MAX_SCRIPT_CANDIDATES,
        step=1,
        key="loomloom_candidate_count",
    )
    duration_seconds = duration_col.number_input(
        tr("Target Script Duration Seconds"),
        min_value=10,
        max_value=600,
        step=10,
        key="loomloom_script_duration_seconds",
    )
    _set_runtime_config("ui", "loomloom_candidate_count", int(candidate_count))
    _set_runtime_config(
        "ui", "loomloom_script_duration_seconds", int(duration_seconds)
    )
    input_signature = _loomloom_script_signature(
        subject=params.video_subject,
        language=params.video_language,
        candidate_count=candidate_count,
        duration_seconds=duration_seconds,
        style=params.video_script_prompt,
        credential_fingerprint=(
            hashlib.sha256(effective_token.encode("utf-8")).hexdigest()
            if effective_token
            else ""
        ),
    )

    if st.button(
        tr("Get LoomLoom Quote"),
        key="loomloom_quote_scripts",
        width="stretch",
        type="secondary",
        icon=":material/request_quote:",
        disabled=not effective_token or bool(st.session_state.get("loomloom_run_id")),
    ):
        if not params.video_subject:
            st.toast(tr("Please Enter the Video Subject First"))
            st.warning(tr("Please Enter the Video Subject First"))
        else:
            try:
                backend = _create_loomloom_script_backend()
                batch = backend.prepare_script_batch(
                    subject=params.video_subject,
                    candidate_count=int(candidate_count),
                    language=params.video_language,
                    duration_seconds=int(duration_seconds),
                    style=params.video_script_prompt,
                )
                quote_result = backend.quote(batch)
            except (loomloom.LoomLoomError, ValueError) as exc:
                logger.warning(f"failed to quote LoomLoom scripts: error={exc}")
                st.error(str(exc))
            else:
                st.session_state["loomloom_script_batch"] = batch
                st.session_state["loomloom_script_quote"] = quote_result
                st.session_state["loomloom_script_input_signature"] = input_signature
                st.session_state["loomloom_client_request_id"] = f"mpt-{uuid4()}"
                st.session_state["loomloom_run_id"] = ""
                st.session_state["loomloom_run_status"] = "quoted"
                st.session_state["loomloom_run_error"] = ""
                st.session_state["loomloom_poll_failure_count"] = 0
                st.session_state["loomloom_poll_retry_after"] = 0.0
                st.session_state["loomloom_poll_paused"] = False
                st.session_state["loomloom_script_candidates"] = ()
                st.session_state["loomloom_candidate_errors"] = ()
                st.session_state["loomloom_confirm_charge"] = False
                logger.info(
                    "LoomLoom script quote ready: "
                    f"tasks={quote_result.task_count}, currency={quote_result.currency}, "
                    f"estimated_payable_t={quote_result.estimated_buyer_payable_t}"
                )

    quote_result = st.session_state.get("loomloom_script_quote")
    batch = st.session_state.get("loomloom_script_batch")
    if quote_result is not None and batch is not None:
        display_amount = (
            quote_result.estimated_buyer_payable_amount
            or f"{quote_result.estimated_buyer_payable_t} T"
        )
        st.success(
            tr(
                "LoomLoom Quote Summary Singular"
                if quote_result.task_count == 1
                else "LoomLoom Quote Summary"
            ).format(
                tasks=quote_result.task_count,
                amount=display_amount,
                currency=quote_result.currency,
            )
        )
        quote_is_current = (
            st.session_state.get("loomloom_script_input_signature") == input_signature
        )
        if not quote_is_current:
            st.warning(tr("LoomLoom Quote Changed Warning"))
        confirm_charge = st.checkbox(
            tr("Confirm LoomLoom Charge"),
            key="loomloom_confirm_charge",
            disabled=not quote_is_current,
        )
        run_in_progress = bool(st.session_state.get("loomloom_run_id"))
        if st.button(
            tr("Run LoomLoom Batch"),
            key="loomloom_execute_scripts",
            width="stretch",
            type="primary",
            disabled=(not quote_is_current or not confirm_charge or run_in_progress),
        ):
            try:
                execution = _create_loomloom_script_backend().execute(
                    batch,
                    client_request_id=st.session_state["loomloom_client_request_id"],
                    listing_version_id=quote_result.listing_version_id,
                    confirm=True,
                )
            except (loomloom.LoomLoomError, ValueError) as exc:
                logger.warning(f"failed to execute LoomLoom scripts: error={exc}")
                st.error(str(exc))
            else:
                st.session_state["loomloom_run_id"] = execution.run_id
                st.session_state["loomloom_run_status"] = "running"
                st.session_state["loomloom_poll_paused"] = False
                # 一次报价只允许启动一次付费批次。后台状态只依赖 run_id，提交
                # 后即可丢弃报价与幂等请求 ID；失败后用户需要重新报价再重试。
                st.session_state["loomloom_script_batch"] = None
                st.session_state["loomloom_script_quote"] = None
                st.session_state["loomloom_script_input_signature"] = ""
                st.session_state["loomloom_client_request_id"] = ""
                logger.info(
                    f"LoomLoom script run submitted: run_id={execution.run_id}, "
                    f"tasks={len(batch.input_rows)}"
                )
                st.toast(tr("LoomLoom Run Submitted"))

    run_error = str(st.session_state.get("loomloom_run_error", "") or "").strip()
    if run_error:
        st.error(tr("LoomLoom Run Failed").format(error=run_error))
    run_id = str(st.session_state.get("loomloom_run_id", "") or "").strip()
    if run_id and st.session_state.get("loomloom_poll_paused", False):
        retry_col, stop_col = st.columns(2)
        if retry_col.button(
            tr("Resume LoomLoom Status Check"),
            key="loomloom_resume_status_check",
            width="stretch",
            type="secondary",
        ):
            st.session_state["loomloom_run_error"] = ""
            st.session_state["loomloom_poll_failure_count"] = 0
            st.session_state["loomloom_poll_retry_after"] = 0.0
            st.session_state["loomloom_poll_paused"] = False
            st.rerun(scope="app")
        if stop_col.button(
            tr("Stop Tracking LoomLoom Run"),
            key="loomloom_stop_tracking_run",
            width="stretch",
            type="secondary",
            help=tr("Stop Tracking LoomLoom Run Help"),
        ):
            # 这里只停止本地状态查询，不声称取消远端执行。用户确认放弃跟踪后
            # 才清理 run_id，下一次付费运行仍需重新报价和确认。
            st.session_state["loomloom_run_id"] = ""
            st.session_state["loomloom_run_error"] = ""
            st.session_state["loomloom_poll_paused"] = False
            st.rerun(scope="app")
    # 只有真实运行中的批次才启动两秒轮询，报价阶段和结果展示阶段不创建
    # 定时 fragment，避免用户停留在页面时产生无意义的网络请求和 rerun。
    if run_id and not st.session_state.get("loomloom_poll_paused", False):
        _render_loomloom_run_progress()
    _render_loomloom_candidates()


def _render_script_settings(panel, params):
    """渲染文案设置并更新生成参数。"""
    with panel:
        with st.container(border=True):
            st.write(tr("Video Script Settings"))
            # 标签行需要容纳“配置大模型”入口，因此无法继续使用 text_area
            # 内置标签。把标签和输入框收进同一个字段容器后，可覆盖内部间距，
            # 同时让该字段与页面上的其它表单控件保持一致的外部节奏。
            with st.container(key="video_subject_field"):
                with st.container(
                    key="video_subject_label_row",
                    horizontal=True,
                    vertical_alignment="center",
                    gap="small",
                ):
                    st.markdown(
                        tr("Video Subject"),
                        help=tr("Video Subject Help"),
                        width="content",
                    )
                    st.button(
                        tr("Configure LLM"),
                        key="open_llm_settings_from_subject",
                        type="tertiary",
                        on_click=_open_settings_dialog,
                        args=("llm",),
                    )
                params.video_subject = st.text_area(
                    tr("Video Subject"),
                    placeholder=tr("Video Subject Placeholder"),
                    height=96,
                    key="video_subject",
                    label_visibility="collapsed",
                ).strip()

            video_languages = [
                (tr("Auto Detect"), ""),
            ]
            for code in support_locales:
                video_languages.append((code, code))

            selected_language_code = stable_selectbox(
                tr("Script Language"),
                options=[value for _, value in video_languages],
                default_value=_saved_ui_choice(
                    "video_language",
                    [value for _, value in video_languages],
                    "",
                ),
                key="script_language_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_languages
                )[value],
            )
            params.video_language = selected_language_code
            _set_runtime_config("ui", "video_language", params.video_language)

            # 使用带 key 的局部容器限定折叠入口样式，保持 expander 的原生交互，
            # 同时避免样式误伤页面顶部的“基础设置”等其他折叠区域。
            with st.container(key="advanced_settings_script"):
                with st.expander(tr("Advanced Script Settings"), expanded=False):
                    script_backend_options = ["local", "loomloom"]
                    script_backend_labels = {
                        "local": tr("Local LLM Script Generation"),
                        "loomloom": tr("Shengsuan Cloud Batch Script Generation"),
                    }
                    script_backend_widget_key = localized_widget_key(
                        "script_generation_backend_select"
                    )
                    current_script_backend = st.session_state.get(
                        script_backend_widget_key,
                        _effective_script_generation_backend(),
                    )
                    script_generation_backend = stable_selectbox(
                        tr("Script Generation Method"),
                        options=script_backend_options,
                        default_value=_effective_script_generation_backend(),
                        key="script_generation_backend_select",
                        format_func=lambda value: script_backend_labels[value],
                        help=_script_generation_method_help(current_script_backend),
                    )
                    _set_runtime_config(
                        "app", "script_generation_backend", script_generation_backend
                    )

                    narrator_options = {
                        "Standard": "standard",
                        "Desi Savage Hindi": "desi_savage_aggressive",
                    }
                    default_narrator = config.ui.get("narrator_preset")
                    if default_narrator not in narrator_options.values():
                        default_narrator = (
                            "desi_savage_aggressive"
                            if params.visual_style == "savage_animated_explainer"
                            else "standard"
                        )
                    narrator_label = stable_selectbox(
                        "Narration Personality",
                        options=list(narrator_options),
                        default_value=next(
                            label for label, value in narrator_options.items()
                            if value == default_narrator
                        ),
                        key="narrator_preset_select",
                    )
                    params.narrator_preset = narrator_options[narrator_label]
                    _set_runtime_config("ui", "narrator_preset", params.narrator_preset)

                    profanity_options = {
                        "Mild": "mild",
                        "Savage — Monetization Aware": "savage_monetization_aware",
                        "Savage Unfiltered": "savage_unfiltered",
                    }
                    default_profanity = config.ui.get(
                        "profanity_level", "savage_monetization_aware"
                    )
                    if default_profanity not in profanity_options.values():
                        default_profanity = "savage_monetization_aware"
                    profanity_label = stable_selectbox(
                        "Profanity Intensity",
                        options=list(profanity_options),
                        default_value=next(
                            label for label, value in profanity_options.items()
                            if value == default_profanity
                        ),
                        key="profanity_level_select",
                        help="Repeated strong profanity may reduce YouTube advertiser suitability.",
                    )
                    params.profanity_level = profanity_options[profanity_label]
                    _set_runtime_config("ui", "profanity_level", params.profanity_level)

                    savage_quality_options = {"Advisory": "advisory", "Strict": "strict"}
                    configured_quality = config.ui.get("savage_quality_policy", "advisory")
                    if configured_quality not in savage_quality_options.values():
                        configured_quality = "advisory"
                    quality_label = stable_selectbox(
                        "Savage creative-quality enforcement",
                        options=list(savage_quality_options),
                        default_value=next(
                            label for label, value in savage_quality_options.items()
                            if value == configured_quality
                        ),
                        key="savage_quality_policy_select",
                        help=("Advisory shows creative warnings and keeps usable scripts. "
                              "Strict rejects drafts that still have creative warnings after one rewrite."),
                    )
                    params.savage_quality_policy = savage_quality_options[quality_label]
                    _set_runtime_config(
                        "ui", "savage_quality_policy", params.savage_quality_policy
                    )

                    params.paragraph_number = st.slider(
                        tr("Script Paragraph Number"),
                        min_value=llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
                        max_value=llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
                        key="paragraph_number_input",
                    )
                    _set_runtime_config(
                        "ui", "paragraph_number", params.paragraph_number
                    )
                    params.video_script_prompt = st.text_area(
                        tr("Custom Script Requirements"),
                        height=100,
                        max_chars=llm.MAX_SCRIPT_PROMPT_LENGTH,
                        placeholder=tr("Custom Script Requirements Placeholder"),
                        key="video_script_prompt",
                    ).strip()
                    _set_runtime_config(
                        "ui", "video_script_prompt", params.video_script_prompt
                    )

                    system_prompt = st.text_area(
                        tr("Custom System Prompt"),
                        height=240,
                        max_chars=llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
                        key="custom_system_prompt",
                    ).strip()
                    # 默认内容由服务层统一维护。界面虽然直接展示默认提示词，但只有
                    # 用户实际修改后才随任务传递，避免历史任务固化旧版本默认规则。
                    params.custom_system_prompt = (
                        ""
                        if system_prompt == llm.DEFAULT_SCRIPT_SYSTEM_PROMPT.strip()
                        else system_prompt
                    )
                    _set_runtime_config(
                        "ui", "custom_system_prompt", params.custom_system_prompt
                    )

                    restore_prompt_col, preview_prompt_col = st.columns(2)
                    if restore_prompt_col.button(
                        tr("Restore Default System Prompt"),
                        key="restore_default_system_prompt",
                        icon=":material/restart_alt:",
                        on_click=reset_script_system_prompt,
                        width="stretch",
                    ):
                        st.toast(tr("Default System Prompt Restored"))
                    if preview_prompt_col.button(
                        tr("Preview Final Prompt"),
                        key="preview_final_script_prompt",
                        icon=":material/preview:",
                        width="stretch",
                    ):
                        render_script_prompt_preview(
                            llm.build_script_prompt(
                                video_subject=params.video_subject,
                                language=params.video_language,
                                paragraph_number=params.paragraph_number,
                                video_script_prompt=params.video_script_prompt,
                                custom_system_prompt=params.custom_system_prompt,
                                visual_style=params.visual_style,
                                narrator_preset=params.narrator_preset,
                                profanity_level=params.profanity_level or "savage_monetization_aware",
                            )
                        )

            # 模型发现只增强视频素材，不改变用户明确选择的文案 Provider。
            if _effective_script_generation_backend() == "loomloom":
                _render_loomloom_script_generation(params)
            else:
                _render_local_script_generation(params)
            params.video_script = st.text_area(
                tr("Video Script"),
                help=tr("Video Script Help"),
                height=180,
                key="video_script",
            )
            quality_review = st.session_state.get("video_script_quality_review")
            if (isinstance(quality_review, dict)
                    and quality_review.get("digest") == hashlib.sha256(
                        params.video_script.strip().encode("utf-8")
                    ).hexdigest()
                    and quality_review.get("warnings")):
                st.warning(
                    "Savage creative-quality notes (Advisory): "
                    + "; ".join(quality_review["warnings"])
                )
            duration_modes = {
                "Flexible": "flexible",
                "Warn only": "warn_only",
                "Strict": "strict",
            }
            saved_duration_mode = config.ui.get("duration_enforcement", "flexible")
            if saved_duration_mode not in duration_modes.values():
                saved_duration_mode = "flexible"
            duration_label = stable_selectbox(
                "Duration enforcement",
                options=list(duration_modes),
                default_value=next(
                    label for label, value in duration_modes.items()
                    if value == saved_duration_mode
                ),
                key="duration_enforcement_select",
                help=("Flexible treats the selected duration as a script-generation target. "
                      "Warn only also reports differences. Strict enforces requested bounds."),
            )
            params.duration_enforcement = duration_modes[duration_label]
            _set_runtime_config("ui", "duration_enforcement", params.duration_enforcement)
            with st.expander("Duration recovery"):
                params.duration_recovery_attempts = st.number_input(
                    "Maximum script/TTS recovery attempts", min_value=0, max_value=2,
                    value=0, step=1, key="duration_recovery_attempts_ui",
                )
                params.duration_recovery_approved = st.checkbox(
                    "Approve additional script, search-term and TTS calls if duration is out of range",
                    key="duration_recovery_approved_ui",
                    disabled=params.duration_recovery_attempts == 0,
                )
            if _effective_script_generation_backend() == "loomloom":
                st.caption(tr("LoomLoom Video Terms Reuse Help"))
            elif st.button(
                tr("Generate Video Keywords"),
                key="auto_generate_terms",
                width="stretch",
                type="secondary",
                icon=":material/auto_awesome:",
            ):
                if not params.video_script:
                    # 视频关键词需要基于文案提取，文案为空时提前提示并跳过模型调用。
                    st.toast(tr("Please Enter the Video Subject"))
                    st.warning(tr("Please Enter the Video Subject"))
                else:
                    with st.spinner(tr("Generating Video Keywords")):
                        terms = _run_llm_read_operation(
                            "generate_terms",
                            lambda app_config_snapshot: llm.generate_terms(
                                params.video_subject,
                                params.video_script,
                                amount=(
                                    max(1, len(tm.emotion_storyboard.build(
                                        tm._represent_script(params, params.video_script).spoken_script,
                                        narrator_preset=params.narrator_preset or "desi_savage_aggressive",
                                        profanity_level=params.profanity_level or "savage_monetization_aware",
                                    )))
                                    if params.visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER
                                    else 8 if params.match_materials_to_script else 5
                                ),
                                match_script_order=(
                                    params.match_materials_to_script
                                    or params.visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER
                                ),
                                app_config=app_config_snapshot,
                                stock_video_style=params.stock_video_style,
                                asmr_categories=params.asmr_categories,
                            ),
                        )
                        if isinstance(terms, str):
                            st.error(tr(terms))
                        elif (params.visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER
                              and len(terms) != len(tm.emotion_storyboard.build(
                                  tm._represent_script(params, params.video_script).spoken_script,
                                  narrator_preset=params.narrator_preset or "desi_savage_aggressive",
                                  profanity_level=params.profanity_level or "savage_monetization_aware",
                              ))):
                            expected = len(tm.emotion_storyboard.build(
                                tm._represent_script(params, params.video_script).spoken_script,
                                narrator_preset=params.narrator_preset or "desi_savage_aggressive",
                                profanity_level=params.profanity_level or "savage_monetization_aware",
                            ))
                            st.error(
                                "Generated " + str(len(terms)) + " keyword(s) for "
                                + str(expected) + " narration beats. Add or repeat keywords "
                                "so each beat has an explicit keyword."
                            )
                        else:
                            st.session_state["video_terms"] = ", ".join(terms)

            params.video_terms = st.text_area(
                tr("Video Keywords"),
                help=tr("Video Keywords Help"),
                key="video_terms",
            )


def _select_stock_media_policy(params):
    configured = str(
        params.stock_media_policy or config.app.get("stock_media_policy", "auto_eligible")
    ).strip().casefold()
    if configured not in {"manual_review", "auto_eligible"}:
        configured = "manual_review"
    labels = {
        "manual_review": "Manual review (hash-bound approval required)",
        "auto_eligible": "Automatic (provider search match; no approval)",
    }
    params.stock_media_policy = stable_selectbox(
        "Stock footage eligibility",
        options=("manual_review", "auto_eligible"),
        default_value=configured,
        key="stock_media_policy_select",
        format_func=labels.get,
        help="Automatic accepts configured provider search matches without human approval. Manual requires hash-bound approval.",
    )
    _set_runtime_config("app", "stock_media_policy", params.stock_media_policy)


def _render_video_settings(panel, params):
    """渲染视频设置并返回本次选择的本地素材。"""
    uploaded_files = []
    with panel:
        with st.container(border=True):
            st.write(tr("Video Settings"))
            video_concat_modes = [
                (tr("Sequential"), "sequential"),
                (tr("Random"), "random"),
            ]
            video_source_labels = {
                "auto": tr("Auto stock providers"),
                "pexels": tr("Pexels"),
                "pixabay": tr("Pixabay"),
                "coverr": tr("Coverr"),
                "wavespeed": tr("WaveSpeed AI Video"),
                "volcengine_seedance": tr("Volcano Engine Seedance"),
                "ofox": tr("OFox AI Video"),
                "metaso_minimax": tr("Metaso MiniMax H3"),
                "muapi": tr("MuAPI AI Video"),
                "loomloom": tr("Shengsuan Cloud AI Video"),
                "openai_image": tr("OpenAI Compatible Text-to-Image"),
                "local": tr("Local file"),
            }
            saved_video_source_name = str(
                config.app.get("video_source", "pexels") or "pexels"
            )
            params.video_source = grouped_selectbox(
                tr("Video Source"),
                groups=(
                    (tr("Stock Video"), VIDEO_SOURCE_GROUPS["stock_video"]),
                    (tr("AI Video"), VIDEO_SOURCE_GROUPS["ai_video"]),
                    (tr("AI Image"), VIDEO_SOURCE_GROUPS["ai_image"]),
                    (tr("Local Material"), VIDEO_SOURCE_GROUPS["local"]),
                ),
                default_value=saved_video_source_name,
                key="video_source_select",
                format_func=video_source_labels.get,
                settings_label=tr("Configure Material Sources"),
                on_settings=_open_material_settings_dialog,
            )
            _set_runtime_config("app", "video_source", params.video_source)

            st.session_state.setdefault(
                "_active_visual_style_preset",
                VISUAL_STYLE_VALUES.get(
                    st.session_state.get("visual_style_select"), params.visual_style
                ),
            )
            selected_visual_style = st.selectbox(
                "Visual Style",
                options=tuple(VISUAL_STYLE_VALUES),
                key="visual_style_select",
                on_change=_on_visual_style_changed,
                help="Savage combines script-matched stock clips with the original character and diagrams. ASMR uses only quality-approved ASMR footage.",
            )
            params.visual_style = VISUAL_STYLE_VALUES[selected_visual_style]
            resolved_style_settings = generation_settings_service.resolve_generation_settings(
                task_override={
                    "visual_style": params.visual_style,
                    "video_subject": params.video_subject,
                    "video_script": params.video_script,
                },
                topic=params.video_subject,
            )
            params.effective_visual_style = resolved_style_settings.effective_visual_style
            if resolved_style_settings.effective_visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER:
                _select_stock_media_policy(params)
            style_labels = {
                "savage_animated_explainer": "Savage Animated Explainer",
                "relevant_stock": "Relevant Stock",
                "asmr_cutting": "ASMR Cutting",
            }
            st.caption(
                "Effective preset: "
                f"{style_labels[resolved_style_settings.effective_visual_style]} · "
                f"{resolved_style_settings.narrator_preset.replace('_', ' ')} · "
                f"{resolved_style_settings.profanity_level.replace('_', ' ')} · "
                f"{resolved_style_settings.caption_preset.replace('_', ' ')} · "
                f"{resolved_style_settings.tts_provider or 'configured voice'} / "
                f"{resolved_style_settings.voice_name or 'configured voice'} · "
                f"preset v{resolved_style_settings.style_preset_version}"
            )
            st.button(
                "Reset to Style Defaults",
                key="reset_visual_style_defaults",
                on_click=_reset_visual_style_defaults,
                type="tertiary",
            )
            if st.session_state.get("video_script_needs_style_review"):
                st.info(
                    "Your existing narration was kept. Its style may differ from this preset; "
                    "use Generate Video Script and Keywords to replace it explicitly."
                )
            params.stock_video_style = (
                "satisfying_asmr_cutting"
                if params.visual_style == "asmr_cutting" else "default"
            )
            if params.visual_style == "asmr_cutting":
                params.asmr_categories = st.multiselect(
                    tr("ASMR Categories"),
                    options=("kinetic_sand", "soap", "foam", "jelly", "crystal", "lava_cube"),
                    default=st.session_state.get(
                        "asmr_categories_select", ["kinetic_sand", "soap"]
                    ),
                    key="asmr_categories_select",
                    help=tr("ASMR Categories Help"),
                )

            loomloom_video_capability = None
            if params.video_source == "loomloom":
                # 尽早读取缓存，使下方画面比例控件直接受当前 Profile 约束。
                # 首次输入 Key 后 Streamlit 会 rerun，此处随即加载一次。
                loomloom_video_capability = _load_loomloom_video_capability(
                    _effective_loomloom_api_token()
                )

            if params.video_source == "wavespeed":
                st.caption(tr("WaveSpeed AI Video Help"))
            if params.video_source == "volcengine_seedance":
                st.caption(tr("Volcano Engine Seedance Help"))
            if params.video_source == "ofox":
                st.caption(f"[OfoxAI]({OFOX_REFERRAL_URL}) · {tr('OFox AI Video Help')}")
            if params.video_source == "metaso_minimax":
                st.caption(tr("Metaso MiniMax H3 Help"))
            if params.video_source == "muapi":
                st.caption(tr("MuAPI AI Video Help"))
            if params.video_source == "local":
                # Streamlit 的文件类型校验对扩展名大小写敏感，这里同时放行大小写两种形式。
                local_file_types = sorted(
                    extension.removeprefix(".")
                    for extension in LOCAL_MATERIAL_EXTENSIONS
                )
                uploaded_files = st.file_uploader(
                    tr("Upload Local Files"),
                    type=local_file_types
                    + [file_type.upper() for file_type in local_file_types],
                    accept_multiple_files=True,
                    key="local_video_materials_uploader",
                )

            # 文案顺序匹配会从关键词生成到最终合成全程保持叙事顺序，因此开启时
            # 顺序拼接是唯一符合实际执行逻辑的选项。同步控件值可避免界面仍显示
            # “随机拼接”，同时保留用户原选择，关闭后自动恢复。
            sync_script_order_concat_mode()
            selected_concat_mode = stable_selectbox(
                tr("Video Concat Mode"),
                options=[value for _, value in video_concat_modes],
                default_value=_saved_ui_choice(
                    "video_concat_mode",
                    [value for _, value in video_concat_modes],
                    VideoConcatMode.random.value,
                ),
                key="video_concat_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_concat_modes
                )[value],
                disabled=bool(st.session_state.get("match_materials_to_script", False)),
            )
            params.video_concat_mode = VideoConcatMode(selected_concat_mode)

            params.match_materials_to_script = st.checkbox(
                tr("Match Materials to Script Order"),
                help=tr("Match Materials to Script Order Help"),
                key="match_materials_to_script",
                on_change=sync_script_order_concat_mode,
            )
            _set_runtime_config(
                "app",
                "match_materials_to_script",
                params.match_materials_to_script,
            )
            # 顺序匹配开启时，sequential 是派生出的强制值，不应覆盖用户在关闭
            # 该功能时选择的拼接偏好；关闭后仍能恢复此前的 random/sequential。
            if not params.match_materials_to_script:
                _set_runtime_config(
                    "ui", "video_concat_mode", params.video_concat_mode.value
                )

            # 视频转场模式
            video_transition_modes = [
                (tr("None"), VideoTransitionMode.none.value),
                (tr("Shuffle"), VideoTransitionMode.shuffle.value),
                (tr("FadeIn"), VideoTransitionMode.fade_in.value),
                (tr("FadeOut"), VideoTransitionMode.fade_out.value),
                (tr("SlideIn"), VideoTransitionMode.slide_in.value),
                (tr("SlideOut"), VideoTransitionMode.slide_out.value),
                (tr("ZoomIn"), VideoTransitionMode.zoom_in.value),
                (tr("ZoomOut"), VideoTransitionMode.zoom_out.value),
            ]
            selected_transition_mode = stable_selectbox(
                tr("Video Transition Mode"),
                options=[value for _, value in video_transition_modes],
                default_value=_saved_ui_choice(
                    "video_transition_mode",
                    [value for _, value in video_transition_modes],
                    VideoTransitionMode.none.value,
                ),
                key="video_transition_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_transition_modes
                )[value],
            )
            params.video_transition_mode = VideoTransitionMode(selected_transition_mode)
            _set_runtime_config(
                "ui",
                "video_transition_mode",
                params.video_transition_mode.value,
            )

            video_aspect_ratios = [
                (tr("Portrait"), VideoAspect.portrait.value),
                (tr("Landscape"), VideoAspect.landscape.value),
            ]
            if loomloom_video_capability is not None:
                ratio_labels = {value: label for label, value in video_aspect_ratios}
                video_aspect_ratios = [
                    (ratio_labels[value], value)
                    for value in loomloom_video_capability.aspect_ratios
                ]
            # Coverr 库 99% 是 16:9 横屏,默认竖屏会让画面被大量黑边包围。
            # 用 source-specific widget key 让每个 source 各自记忆 aspect 选择:
            #   - 首次切到 coverr → 默认 Landscape(index=1)
            #   - 其他 source 沿用 Portrait(index=0)
            #   - 用户在某 source 下手动改过 aspect,session_state 会记住,
            #     下次回到同一 source 时尊重用户选择,不会再被强制覆盖。
            default_aspect_index = 1 if params.video_source == "coverr" else 0
            video_aspect_values = [value for _, value in video_aspect_ratios]
            video_aspect_config_key = f"video_aspect_{params.video_source}"
            selected_aspect_ratio = stable_selectbox(
                tr("Video Ratio"),
                options=video_aspect_values,
                default_value=_saved_ui_choice(
                    video_aspect_config_key,
                    video_aspect_values,
                    video_aspect_ratios[default_aspect_index][1],
                ),
                key=f"video_aspect_for_{params.video_source}",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_aspect_ratios
                )[value],
            )
            params.video_aspect = VideoAspect(selected_aspect_ratio)
            _set_runtime_config(
                "ui", video_aspect_config_key, params.video_aspect.value
            )

            video_fit_modes = [
                (tr("Fill and Crop"), VideoFitMode.cover.value),
                (tr("Fit with Black Bars"), VideoFitMode.contain.value),
            ]
            selected_fit_mode = stable_selectbox(
                tr("Video Fit Mode"),
                options=[value for _, value in video_fit_modes],
                default_value=_saved_ui_choice(
                    "video_fit_mode",
                    [value for _, value in video_fit_modes],
                    VideoFitMode.cover.value,
                ),
                key="video_fit_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_fit_modes
                )[value],
                help=tr("Video Fit Mode Help"),
            )
            params.video_fit_mode = VideoFitMode(selected_fit_mode)
            _set_runtime_config(
                "ui", "video_fit_mode", params.video_fit_mode.value
            )

            if params.video_source in {"local", "openai_image"}:
                image_motion_labels = {
                    ImageMotion.static.value: tr("Static Image"),
                    ImageMotion.zoom.value: tr("Slow Zoom"),
                }
                params.image_motion = ImageMotion(
                    stable_selectbox(
                        tr("Image Motion"),
                        options=list(image_motion_labels),
                        default_value=_saved_ui_choice(
                            "image_motion", list(image_motion_labels), ImageMotion.zoom.value,
                            # 生成任务持锁时，切换语言或素材来源会重建控件；
                            # 此时必须恢复尚未落盘的最新选择，不能写回旧配置值。
                            section=config.snapshot_config_with_pending(config.ui),
                        ),
                        key="image_motion_select",
                        format_func=image_motion_labels.get,
                        help=tr("Image Motion Help"),
                    )
                )
                _set_runtime_config("ui", "image_motion", params.image_motion.value)

            # MiniMax H3 的远端时长范围是 4～15 秒。选择秘塔时使用完整能力
            # 范围，既避免 2/3 秒被按 4 秒计费，也让 WebUI 与 CLI、服务层一致。
            if params.video_source == "metaso_minimax":
                video_clip_durations = list(
                    range(
                        metaso_minimax.DEFAULT_MIN_DURATION_SECONDS,
                        metaso_minimax.DEFAULT_MAX_DURATION_SECONDS + 1,
                    )
                )
            elif params.video_source == "muapi":
                video_clip_durations = list(
                    range(
                        muapi.DEFAULT_MIN_DURATION_SECONDS,
                        muapi.DEFAULT_MAX_DURATION_SECONDS + 1,
                    )
                )
            else:
                video_clip_durations = [2, 3, 4, 5, 6, 7, 8, 9, 10]
            params.video_clip_duration = stable_selectbox(
                tr("Clip Duration"),
                options=video_clip_durations,
                default_value=_saved_ui_choice(
                    "video_clip_duration",
                    video_clip_durations,
                    5
                    if params.video_source in {"metaso_minimax", "muapi"}
                    else 3,
                ),
                key="video_clip_duration_select",
                help=tr("Clip Duration Help"),
            )
            _set_runtime_config(
                "ui", "video_clip_duration", params.video_clip_duration
            )
            clip_speed_key = localized_widget_key("video_clip_speed_slider")
            # session_state 可能来自旧任务、API 参数或旧版页面状态。控件创建前
            # 统一归一化，既保留合法选择，也确保 slider 始终收到 0.5～2.0
            # 范围内的有限浮点数。
            st.session_state[clip_speed_key] = utils.normalize_clip_speed(
                st.session_state.get(
                    clip_speed_key,
                    _saved_ui_number("video_clip_speed", 1.0, 0.5, 2.0),
                )
            )
            params.video_clip_speed = st.slider(
                tr("Clip Speed"),
                min_value=0.5,
                max_value=2.0,
                step=0.05,
                format="%.2fx",
                key=clip_speed_key,
                help=tr("Clip Speed Help"),
            )
            _set_runtime_config("ui", "video_clip_speed", params.video_clip_speed)
            video_count_options = [1, 2, 3, 4, 5]
            params.video_count = stable_selectbox(
                tr("Number of Videos Generated Simultaneously"),
                options=video_count_options,
                default_value=_saved_ui_choice(
                    "video_count", video_count_options, 1
                ),
                key="video_count_select",
            )
            _set_runtime_config("ui", "video_count", params.video_count)

            video_codec_options = [
                (tr("Default Video Encoder"), DEFAULT_VIDEO_CODEC_OPTION),
                ("libx264 (CPU)", "libx264"),
                ("NVIDIA NVENC (h264_nvenc)", "h264_nvenc"),
                ("AMD AMF (h264_amf)", "h264_amf"),
                ("Intel QSV (h264_qsv)", "h264_qsv"),
                ("Windows MediaFoundation (h264_mf)", "h264_mf"),
                ("macOS VideoToolbox (h264_videotoolbox)", "h264_videotoolbox"),
            ]
            saved_video_codec = config.app.get(
                "video_codec", DEFAULT_VIDEO_CODEC_OPTION
            )
            saved_video_codec_values = [item[1] for item in video_codec_options]
            if saved_video_codec not in saved_video_codec_values:
                # 旧版本或手工配置可能留下无效值。UI 回到“默认”而不是替用户
                # 固定某个编码器，后端仍会按稳定策略解析为 libx264。
                saved_video_codec = DEFAULT_VIDEO_CODEC_OPTION
            selected_video_codec = stable_selectbox(
                tr("Video Encoder"),
                options=saved_video_codec_values,
                default_value=saved_video_codec,
                key="video_encoder_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_codec_options
                )[value],
                help=tr("Video Encoder Help"),
            )
            if selected_video_codec == DEFAULT_VIDEO_CODEC_OPTION:
                # 默认模式不持久化具体编码器，让配置表达“跟随项目默认值”。
                _delete_runtime_config("app", "video_codec")
            else:
                _set_runtime_config("app", "video_codec", selected_video_codec)

            concurrency_options = [1, 2, 4, 6, 8]
            if params.video_source in {"pexels", "pixabay", "coverr"}:
                selected_material_concurrency = stable_selectbox(
                    tr("Material Concurrency"),
                    options=concurrency_options,
                    default_value=_saved_ui_choice(
                        "material_concurrency", concurrency_options, 1, config.app
                    ),
                    key="material_concurrency_select",
                    help=tr("Material Concurrency Help"),
                )
                _set_runtime_config(
                    "app", "material_concurrency", selected_material_concurrency
                )

            selected_clip_concurrency = stable_selectbox(
                tr("Clip Rendering Concurrency"),
                options=concurrency_options,
                default_value=_saved_ui_choice(
                    "video_clip_concurrency", concurrency_options, 1, config.app
                ),
                key="clip_rendering_concurrency_select",
                help=tr("Clip Rendering Concurrency Help"),
            )
            _set_runtime_config(
                "app", "video_clip_concurrency", selected_clip_concurrency
            )

            if params.video_source == "loomloom":
                _render_loomloom_video_settings(params)

            if params.video_source == "wavespeed":
                _render_wavespeed_video_settings(params)
            if params.video_source == "volcengine_seedance":
                _render_seedance_video_settings(params)
            if params.video_source == "ofox":
                _render_ofox_video_settings(params)
            if params.video_source == "metaso_minimax":
                _render_metaso_minimax_video_settings(params)
            if params.video_source == "muapi":
                _render_muapi_video_settings(params)
    return uploaded_files


def _render_wavespeed_video_settings(params):
    """
    渲染 WaveSpeed 生成数量估算与计费确认。

    生成按条计费，提交前必须让用户看到大致会生成多少段。估算完全在本地
    完成：用配音时长估算区间除以片段时长得到需要覆盖的片段数。素材流程
    本身按需逐段生成、凑够所需时长即停，因此实际生成数以运行时为准，
    估算只用于量级提示，不参与任务执行。
    """
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""),
        params.voice_rate,
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("WaveSpeed Billing Notice").format(min=min_clips, max=max_clips)
        )
    else:
        st.warning(tr("WaveSpeed Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm WaveSpeed Charge"),
        key="wavespeed_confirm_charge",
        help=tr("Confirm WaveSpeed Charge Help"),
    )


def _render_seedance_video_settings(params):
    """展示预计付费任务数量，并要求用户明确确认方舟生成费用。"""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("Volcano Engine Seedance Billing Notice").format(
                min=min_clips, max=max_clips
            )
        )
    else:
        st.warning(tr("Volcano Engine Seedance Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm Volcano Engine Seedance Charge"),
        key="volcengine_seedance_confirm_charge",
        help=tr("Confirm Volcano Engine Seedance Charge Help"),
    )


def _render_ofox_video_settings(params):
    """展示预计付费任务数量，并要求用户明确确认 OFox 生成费用。"""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("OFox Billing Notice").format(min=min_clips, max=max_clips)
        )
    else:
        st.warning(tr("OFox Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm OFox Charge"),
        key="ofox_confirm_charge",
        help=tr("Confirm OFox Charge Help"),
    )


def _render_metaso_minimax_video_settings(params):
    """展示预计付费任务数量，并要求用户确认秘塔 MiniMax 生成费用。"""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    voice_mode = st.session_state.get(
        localized_widget_key("voice_mode_control"),
        config.ui.get("voice_mode"),
    )
    if voice_mode == VOICE_MODE_UPLOAD:
        # 视频设置渲染在音频设置之前，此时无法可靠读取本轮新上传文件的实际
        # 时长。上传模式不再展示按脚本文字推算的数字，避免用户误以为一个
        # 5 秒音频也会按较长文案创建多个付费任务；运行时仍以文件真实时长为准。
        st.warning(
            tr("Metaso MiniMax Billing Notice Uploaded Audio").format(
                resolution=str(
                    config.app.get(
                        "metaso_minimax_resolution",
                        metaso_minimax.DEFAULT_RESOLUTION,
                    )
                    or metaso_minimax.DEFAULT_RESOLUTION
                ),
                duration=clip_duration,
                count=video_count,
            )
        )
    elif estimated_range := _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    ):
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("Metaso MiniMax Billing Notice").format(
                min=min_clips,
                max=max_clips,
                resolution=str(
                    config.app.get(
                        "metaso_minimax_resolution",
                        metaso_minimax.DEFAULT_RESOLUTION,
                    )
                    or metaso_minimax.DEFAULT_RESOLUTION
                ),
            )
        )
    else:
        st.warning(tr("Metaso MiniMax Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm Metaso MiniMax Charge"),
        key="metaso_minimax_confirm_charge",
        help=tr("Confirm Metaso MiniMax Charge Help"),
    )


def _render_muapi_video_settings(params):
    """Show an estimated MuAPI task count and require explicit billing consent."""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    if estimated_range := _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    ):
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("MuAPI Billing Notice").format(min=min_clips, max=max_clips)
        )
    else:
        st.warning(tr("MuAPI Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm MuAPI Charge"),
        key="muapi_confirm_charge",
        help=tr("Confirm MuAPI Charge Help"),
    )


def _estimate_voiceover_duration_range(
    text: str, voice_rate: float
) -> tuple[float, float] | None:
    """
    在本地估算完整配音时长，返回保守的上下界秒数。

    该估算只用于帮助用户在调用付费 TTS 前判断文案量级，不参与任务执行。
    中文、日文和韩文按字符速度估算，其它使用空格分词的语言按单词速度估算，
    再计入常见标点停顿。不同 Provider、音色和语气会造成实际偏差，因此界面
    必须展示区间而不是伪精确的单一结果。
    """
    normalized_text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized_text:
        return None

    script_chars = re.findall(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        normalized_text,
    )
    remaining_text = re.sub(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        " ",
        normalized_text,
    )
    words = re.findall(r"\b[\w]+(?:[-'’][\w]+)*\b", remaining_text, re.UNICODE)
    punctuation_count = len(re.findall(r"[,，.。!?！？;；:：]", normalized_text))

    # 4.2 字/秒和 2.6 词/秒接近日常解说语速；标点按 0.12 秒加入轻微停顿。
    # voice_rate 只作为估算修正项。部分生成式 TTS 不严格执行倍率，所以最终
    # 仍保留 ±15% 区间，避免让用户误以为该值等同于服务端真实结果。
    base_seconds = len(script_chars) / 4.2 + len(words) / 2.6 + punctuation_count * 0.12
    if base_seconds <= 0:
        return None

    normalized_rate = max(float(voice_rate or 1.0), 0.1)
    estimated_seconds = base_seconds / normalized_rate
    return (
        round(max(estimated_seconds * 0.85, 1.0), 1),
        round(max(estimated_seconds * 1.15, 1.0), 1),
    )


def _get_voice_preview_sample(voice_name: str) -> str:
    """返回适合当前音色的短试听文案，不使用用户的完整视频文案。"""
    # ElevenLabs 音色缺少明确语言字段时，根据展示名称中的越南语字符选择
    # 试听文案，避免用明显不匹配的语言判断音色效果。
    if voice.is_elevenlabs_voice(voice_name):
        parts = voice_name.split(":", 2)
        display = parts[2] if len(parts) >= 3 else ""
        vietnamese_chars = set("àáâãèéêìíòóôõùúýăđơưÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂĐƠƯ")
        if any(char in vietnamese_chars for char in display):
            return "Xin chào, đây là đoạn âm thanh thử nghiệm giọng nói."
    return tr("Voice Example")


def _voice_preview_fingerprint(
    *,
    preview_type: str,
    content: str,
    tts_server: str,
    voice_name: str,
    voice_rate: float,
    voice_volume: float,
    provider_signature: dict,
    synthesis_fingerprint: str = "",
) -> str:
    """生成试听缓存指纹，任一配音参数变化后自动让旧试听结果失效。"""
    payload = {
        "preview_type": preview_type,
        "content": content,
        "tts_server": tts_server,
        "voice_name": voice_name,
        "voice_rate": voice_rate,
        "voice_volume": voice_volume,
        "provider_signature": provider_signature,
        "synthesis_fingerprint": synthesis_fingerprint,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _credential_signature(value: str) -> str:
    """
    生成只用于缓存失效判断的凭证摘要。

    摘要不会写入配置、日志或任务文件。用户修改 API Key 后摘要会变化，从而
    强制重新调用当前配音服务，避免旧试听缓存让无效的新凭证看起来可用。
    """
    normalized_value = str(value or "")
    if not normalized_value:
        return ""
    return hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()


def _get_voxcpm_reference_audio() -> bytes | None:
    """Return the current session's normalized reference audio, if any."""
    payload = st.session_state.get(VOXCPM_REFERENCE_AUDIO_SESSION_KEY)
    if not isinstance(payload, dict):
        return None
    audio_bytes = payload.get("audio_bytes")
    return bytes(audio_bytes) if isinstance(audio_bytes, bytes) else None


def _get_voxcpm_reference_audio_digest() -> str:
    payload = st.session_state.get(VOXCPM_REFERENCE_AUDIO_SESSION_KEY)
    if not isinstance(payload, dict):
        return ""
    digest = payload.get("audio_digest")
    return str(digest) if isinstance(digest, str) else ""


def _get_voxcpm_prompt_audio() -> bytes | None:
    payload = st.session_state.get(VOXCPM_PROMPT_AUDIO_SESSION_KEY)
    if not isinstance(payload, dict):
        return None
    audio_bytes = payload.get("audio_bytes")
    return bytes(audio_bytes) if isinstance(audio_bytes, bytes) else None


def _get_voxcpm_prompt_audio_digest() -> str:
    payload = st.session_state.get(VOXCPM_PROMPT_AUDIO_SESSION_KEY)
    if not isinstance(payload, dict):
        return ""
    digest = payload.get("audio_digest")
    return str(digest) if isinstance(digest, str) else ""


def _get_voxcpm_prompt_text() -> str:
    return str(st.session_state.get(VOXCPM_PROMPT_TEXT_SESSION_KEY, "") or "").strip()


def _clear_voxcpm_separate_prompt_audio() -> None:
    st.session_state.pop("voxcpm_prompt_audio_uploader", None)
    st.session_state.pop(VOXCPM_PROMPT_AUDIO_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY, None)


def _clear_voxcpm_prompt_state() -> None:
    _clear_voxcpm_separate_prompt_audio()
    _clear_voxcpm_prompt_transcript()
    st.session_state.pop(VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_PROMPT_EXAMPLE_MODE_SESSION_KEY, None)


def _clear_voxcpm_prompt_transcript() -> None:
    st.session_state.pop(VOXCPM_PROMPT_TEXT_SESSION_KEY, None)


def _sync_voxcpm_prompt_example_mode(use_separate_prompt_audio: bool) -> None:
    """Invalidate the transcript whenever its effective example changes mode."""
    previous_mode = st.session_state.get(VOXCPM_PROMPT_EXAMPLE_MODE_SESSION_KEY)
    current_mode = bool(use_separate_prompt_audio)
    if previous_mode is not None and bool(previous_mode) != current_mode:
        _clear_voxcpm_prompt_transcript()
    st.session_state[VOXCPM_PROMPT_EXAMPLE_MODE_SESSION_KEY] = current_mode


def _get_voxcpm_effective_prompt_audio() -> bytes | None:
    if not st.session_state.get(VOXCPM_HIGH_FIDELITY_SESSION_KEY, False):
        return None
    if st.session_state.get(VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY, False):
        return _get_voxcpm_prompt_audio()
    return _get_voxcpm_reference_audio()


def _get_voxcpm_effective_prompt_audio_digest() -> str:
    if not st.session_state.get(VOXCPM_HIGH_FIDELITY_SESSION_KEY, False):
        return ""
    if st.session_state.get(VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY, False):
        return _get_voxcpm_prompt_audio_digest()
    return _get_voxcpm_reference_audio_digest()


def _get_voxcpm_prompt_validation_error() -> str:
    if not st.session_state.get(VOXCPM_HIGH_FIDELITY_SESSION_KEY, False):
        return ""
    upload_error = st.session_state.get(VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY)
    if upload_error:
        return str(upload_error)
    prompt_text = _get_voxcpm_prompt_text()
    if not prompt_text:
        return tr("VoxCPM Prompt Text Required")
    if (
        st.session_state.get(VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY, False)
        and not _get_voxcpm_prompt_audio()
    ):
        return tr("VoxCPM Prompt Pair Required")
    return ""


def _get_voxcpm_preview_validation_error(
    selected_tts_server: str,
    voice_name: str,
) -> str:
    if selected_tts_server != "voxcpm" and not voice.is_voxcpm_voice(voice_name):
        return ""
    reference_error = st.session_state.get(VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY)
    if reference_error:
        return str(reference_error)
    return _get_voxcpm_prompt_validation_error()


def _sync_voxcpm_reference_audio(uploaded_file) -> bytes | None:
    """Normalize a new upload once and keep only bounded WAV bytes in session."""
    if uploaded_file is None:
        st.session_state.pop(VOXCPM_REFERENCE_AUDIO_SESSION_KEY, None)
        st.session_state.pop(VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY, None)
        _clear_voxcpm_prompt_state()
        st.session_state.pop(VOXCPM_HIGH_FIDELITY_SESSION_KEY, None)
        return None

    upload_size = int(getattr(uploaded_file, "size", 0) or 0)
    if upload_size <= 0:
        st.session_state.pop(VOXCPM_REFERENCE_AUDIO_SESSION_KEY, None)
        _clear_voxcpm_prompt_state()
        st.session_state[VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY] = tr(
            "VoxCPM Reference Audio Empty"
        )
        return None
    if upload_size > voice.VOXCPM_REFERENCE_AUDIO_MAX_UPLOAD_BYTES:
        st.session_state.pop(VOXCPM_REFERENCE_AUDIO_SESSION_KEY, None)
        _clear_voxcpm_prompt_state()
        st.session_state[VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY] = tr(
            "VoxCPM Reference Audio Upload Too Large"
        ).format(limit=voice.VOXCPM_REFERENCE_AUDIO_MAX_UPLOAD_BYTES // (1024 * 1024))
        return None

    previous_digest = _get_voxcpm_reference_audio_digest()
    raw_audio = uploaded_file.getvalue()
    upload_digest = hashlib.sha256(raw_audio).hexdigest()
    cached = st.session_state.get(VOXCPM_REFERENCE_AUDIO_SESSION_KEY)
    if isinstance(cached, dict) and cached.get("upload_digest") == upload_digest:
        st.session_state.pop(VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY, None)
        return _get_voxcpm_reference_audio()

    try:
        with st.spinner(tr("Validating VoxCPM Reference Audio")):
            wav_audio = voice.prepare_voxcpm_reference_audio(
                raw_audio,
                Path(str(getattr(uploaded_file, "name", ""))).suffix,
            )
    except ValueError as exc:
        # Do not retain a prior clip after the user selects a replacement that
        # fails validation. Otherwise preview and generation could silently use
        # a different person's voice than the one currently shown in the UI.
        st.session_state.pop(VOXCPM_REFERENCE_AUDIO_SESSION_KEY, None)
        _clear_voxcpm_prompt_state()
        st.session_state[VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY] = str(exc)
        return None

    st.session_state[VOXCPM_REFERENCE_AUDIO_SESSION_KEY] = {
        "upload_digest": upload_digest,
        "audio_digest": hashlib.sha256(wav_audio).hexdigest(),
        "audio_bytes": wav_audio,
    }
    if (
        previous_digest != hashlib.sha256(wav_audio).hexdigest()
        and not st.session_state.get(VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY, False)
    ):
        _clear_voxcpm_prompt_transcript()
    st.session_state.pop(VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY, None)
    return bytes(wav_audio)


def _sync_voxcpm_prompt_audio(uploaded_file) -> bytes | None:
    """Normalize the optional delivery example without persisting its content."""
    if uploaded_file is None:
        st.session_state.pop(VOXCPM_PROMPT_AUDIO_SESSION_KEY, None)
        st.session_state.pop(VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY, None)
        return None

    upload_size = int(getattr(uploaded_file, "size", 0) or 0)
    if upload_size <= 0:
        st.session_state.pop(VOXCPM_PROMPT_AUDIO_SESSION_KEY, None)
        st.session_state[VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY] = tr(
            "VoxCPM Prompt Audio Empty"
        )
        return None
    if upload_size > voice.VOXCPM_REFERENCE_AUDIO_MAX_UPLOAD_BYTES:
        st.session_state.pop(VOXCPM_PROMPT_AUDIO_SESSION_KEY, None)
        st.session_state[VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY] = tr(
            "VoxCPM Prompt Audio Upload Too Large"
        ).format(limit=voice.VOXCPM_REFERENCE_AUDIO_MAX_UPLOAD_BYTES // (1024 * 1024))
        return None

    previous_digest = _get_voxcpm_prompt_audio_digest()
    raw_audio = uploaded_file.getvalue()
    upload_digest = hashlib.sha256(raw_audio).hexdigest()
    cached = st.session_state.get(VOXCPM_PROMPT_AUDIO_SESSION_KEY)
    if isinstance(cached, dict) and cached.get("upload_digest") == upload_digest:
        st.session_state.pop(VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY, None)
        return _get_voxcpm_prompt_audio()

    try:
        with st.spinner(tr("Validating VoxCPM Prompt Audio")):
            wav_audio = voice.prepare_voxcpm_reference_audio(
                raw_audio,
                Path(str(getattr(uploaded_file, "name", ""))).suffix,
            )
    except ValueError as exc:
        st.session_state.pop(VOXCPM_PROMPT_AUDIO_SESSION_KEY, None)
        st.session_state[VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY] = str(exc)
        return None

    st.session_state[VOXCPM_PROMPT_AUDIO_SESSION_KEY] = {
        "upload_digest": upload_digest,
        "audio_digest": hashlib.sha256(wav_audio).hexdigest(),
        "audio_bytes": wav_audio,
    }
    if previous_digest != hashlib.sha256(wav_audio).hexdigest():
        _clear_voxcpm_prompt_transcript()
    st.session_state.pop(VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY, None)
    return bytes(wav_audio)


def _get_voice_preview_provider_signature(tts_server: str) -> dict:
    """
    返回会影响试听结果的非敏感 Provider 配置。

    API Key 只以单向摘要参与缓存指纹，原始凭证不会进入缓存或日志。模型、
    服务地址、区域或凭证发生变化时都必须重新生成试听，否则界面可能继续播放
    旧 Provider 配置下的音频，让用户误判当前设置已经生效。
    """
    if tts_server == "azure-tts-v2":
        return {
            "speech_region": config.azure.get("speech_region", ""),
            "credential": _credential_signature(config.azure.get("speech_key", "")),
        }
    if tts_server == "siliconflow":
        return {
            "credential": _credential_signature(config.siliconflow.get("api_key", ""))
        }
    if tts_server == "gemini-tts":
        return {
            "credential": _credential_signature(config.app.get("gemini_api_key", ""))
        }
    if tts_server == "mimo-tts":
        return {"credential": _credential_signature(config.app.get("mimo_api_key", ""))}
    if tts_server == "minimax-tts":
        return {
            "base_url": voice.get_minimax_tts_endpoint(),
            "model_id": config.minimax_tts.get("model_id", ""),
            "voice_id": config.minimax_tts.get("voice_id", ""),
            "credential": _credential_signature(voice.get_minimax_tts_api_key()),
        }
    if tts_server == "elevenlabs":
        return {
            "model_id": config.elevenlabs.get("model_id", ""),
            "credential": _credential_signature(config.elevenlabs.get("api_key", "")),
        }
    if tts_server == "chatterbox":
        return {
            "base_url": config.chatterbox.get("base_url", ""),
            "model_id": config.chatterbox.get("model_id", ""),
            "credential": _credential_signature(config.chatterbox.get("api_key", "")),
        }
    if tts_server == "kokoro":
        return {
            "base_url": config.kokoro.get("base_url", ""),
            "model_id": config.kokoro.get("model_id", ""),
            "credential": _credential_signature(config.kokoro.get("api_key", "")),
        }
    if tts_server == "voxcpm":
        return {
            "base_url": config.voxcpm.get("base_url", ""),
            "model_id": config.voxcpm.get("model_id", ""),
            "voice_id": config.voxcpm.get("voice_id", "default"),
            "credential": _credential_signature(config.voxcpm.get("api_key", "")),
            "reference_audio": _get_voxcpm_reference_audio_digest(),
            "prompt_audio": _get_voxcpm_effective_prompt_audio_digest(),
            "prompt_text": _credential_signature(
                _get_voxcpm_prompt_text()
                if st.session_state.get(VOXCPM_HIGH_FIDELITY_SESSION_KEY, False)
                else ""
            ),
        }
    return {}


def _synthesize_voice_preview(
    *,
    content: str,
    preview_type: str,
    selected_tts_server: str,
    voice_name: str,
    voice_rate: float,
    voice_volume: float,
    tts_model: str = "gemini-3.8-flash-tts",
    tts_provider: str = "",
    emotion_plan: list[dict] | None = None,
    synthesis_fingerprint: str = "",
    voxcpm_reference_audio: bytes | None = None,
    voxcpm_prompt_audio: bytes | None = None,
    voxcpm_prompt_text: str = "",
) -> dict | None:
    """生成一次试听并转为内存缓存，临时文件不会跨会话长期保留。"""
    if selected_tts_server == "chatterbox":
        _sync_chatterbox_config_from_session_state()
    if selected_tts_server == "kokoro":
        _sync_kokoro_config_from_session_state()

    temp_dir = utils.storage_dir("temp", create=True)
    audio_file = os.path.join(temp_dir, f"tmp-voice-{str(uuid4())}.mp3")
    logger.info(
        f"generating {preview_type} voice preview: "
        f"voice={voice_name}, rate={voice_rate}, volume={voice_volume}, "
        f"text_length={len(content)}"
    )
    try:
        with config.try_runtime_config_lock() as lock_acquired:
            if not lock_acquired:
                return {"busy": True}
            tts_kwargs = {
                "text": content,
                "voice_name": voice_name,
                "voice_rate": voice_rate,
                "voice_file": audio_file,
                "voice_volume": voice_volume,
                "tts_model": tts_model,
                "tts_provider": tts_provider or selected_tts_server,
            }
            if emotion_plan:
                tts_kwargs["emotion_plan"] = emotion_plan
            if voxcpm_reference_audio is not None:
                tts_kwargs["voxcpm_reference_audio"] = voxcpm_reference_audio
            if voxcpm_prompt_audio is not None:
                tts_kwargs["voxcpm_prompt_audio"] = voxcpm_prompt_audio
                tts_kwargs["voxcpm_prompt_text"] = voxcpm_prompt_text
            sub_maker = voice.tts(**tts_kwargs)
        if not sub_maker or not os.path.exists(audio_file):
            logger.error(f"{preview_type} voice preview did not produce an audio file")
            return None

        with open(audio_file, "rb") as file:
            audio_bytes = file.read()
        if not audio_bytes:
            logger.error(f"voice preview audio file is empty: {audio_file}")
            return None

        duration = voice.get_audio_duration(audio_file)
        if (
            not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            logger.warning(
                f"voice preview duration is unavailable: "
                f"preview_type={preview_type}, voice={voice_name}"
            )
            duration = None

        return {
            "audio_bytes": audio_bytes,
            "mime_type": _detect_audio_mime(audio_file, audio_bytes),
            "duration": duration,
            "preview_type": preview_type,
            "sub_maker": sub_maker,
            # 让位于音频面板之前的视频面板只采用与当前设置完全匹配的
            # 完整试听时长；短试听或旧文案绝不能改变推荐素材数。
            "content_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "tts_server": selected_tts_server,
            "voice_name": voice_name,
            "voice_rate": float(voice_rate),
            "tts_model": tts_model,
            "synthesis_fingerprint": synthesis_fingerprint,
        }
    finally:
        # 浏览器播放器使用内存字节，文件读取完即可清理，避免频繁试听积累临时文件。
        try:
            os.remove(audio_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # 清理失败不应覆盖真正的 TTS 响应或异常，但需要保留路径和系统错误，
            # 方便排查权限、只读文件系统等环境问题。
            logger.warning(
                f"failed to delete voice preview file {audio_file}: {str(exc)}"
            )


def _render_voice_preview(params, friendly_names, selected_tts_server, voice_name):
    """渲染低成本短试听、完整文案时长估算和按需完整配音预览。"""
    if not friendly_names:
        return

    script_content = str(params.video_script or "").strip()
    spoken_content = hindi_script.build_representations(
        script_content, params.video_language,
        pronunciation_overrides=params.pronunciation_overrides,
    ).spoken_script
    params.tts_provider = selected_tts_server
    resolved_settings = generation_settings_service.resolve_generation_settings(
        task_override=params.model_dump(mode="python"), topic=params.video_subject
    )
    emotion_plan = tm.emotion_storyboard.build(
        spoken_content,
        narrator_preset=resolved_settings.narrator_preset,
        profanity_level=resolved_settings.profanity_level,
    ) if script_content else []
    full_synthesis_fingerprint = (
        generation_settings_service.synthesis_fingerprint(
            spoken_content, resolved_settings, emotion_plan
        ) if script_content else ""
    )
    sample_synthesis_fingerprint = generation_settings_service.synthesis_fingerprint(
        _get_voice_preview_sample(voice_name), resolved_settings, []
    )
    estimated_range = _estimate_voiceover_duration_range(
        spoken_content,
        params.voice_rate,
    )
    if estimated_range:
        st.caption(
            tr("Estimated Voiceover Duration").format(
                min=estimated_range[0],
                max=estimated_range[1],
            )
        )
    else:
        st.caption(tr("Voiceover Script Required"))

    sample_content = _get_voice_preview_sample(voice_name)
    provider_signature = _get_voice_preview_provider_signature(selected_tts_server)
    preview_validation_error = _get_voxcpm_preview_validation_error(
        selected_tts_server,
        voice_name,
    )
    preview_columns = st.columns(2)
    short_preview_requested = preview_columns[0].button(
        tr("Play Voice"),
        key="play_voice_button",
        icon=":material/graphic_eq:",
        width="stretch",
        disabled=bool(preview_validation_error),
    )
    full_preview_requested = preview_columns[1].button(
        tr("Generate Full Voiceover Preview"),
        key="generate_full_voiceover_preview_button",
        icon=":material/article:",
        help=tr("Full Voiceover Preview Cost Hint"),
        width="stretch",
        disabled=not bool(script_content) or bool(preview_validation_error),
    )

    preview_type = ""
    preview_content = ""
    if short_preview_requested:
        preview_type = "sample"
        preview_content = sample_content
    elif full_preview_requested:
        preview_type = "full"
        preview_content = spoken_content

    sample_fingerprint = _voice_preview_fingerprint(
        preview_type="sample",
        content=sample_content,
        tts_server=selected_tts_server,
        voice_name=voice_name,
        voice_rate=params.voice_rate,
        voice_volume=params.voice_volume,
        provider_signature=provider_signature,
        synthesis_fingerprint=sample_synthesis_fingerprint,
    )
    full_fingerprint = (
        _voice_preview_fingerprint(
            preview_type="full",
            content=spoken_content,
            tts_server=selected_tts_server,
            voice_name=voice_name,
            voice_rate=params.voice_rate,
            voice_volume=params.voice_volume,
            provider_signature=provider_signature,
            synthesis_fingerprint=full_synthesis_fingerprint,
        )
        if script_content
        else ""
    )

    if preview_type:
        if preview_type == "full":
            alignment_errors = tm._alignment_preflight_errors(
                params, spoken_content, script_content
            )
            if alignment_errors:
                message = "; ".join(item["message"] for item in alignment_errors)
                st.error(f"Voice preview blocked before synthesis: {message}")
                return
        requested_fingerprint = (
            sample_fingerprint if preview_type == "sample" else full_fingerprint
        )
        cached_preview = st.session_state.get("voice_preview_audio")
        if (
            not cached_preview
            or cached_preview.get("fingerprint") != requested_fingerprint
        ):
            try:
                with st.spinner(tr("Synthesizing Voice")):
                    preview_result = _synthesize_voice_preview(
                        content=preview_content,
                        preview_type=preview_type,
                        selected_tts_server=selected_tts_server,
                        voice_name=voice_name,
                        voice_rate=params.voice_rate,
                        voice_volume=params.voice_volume,
                        tts_model=resolved_settings.tts_model,
                        tts_provider=resolved_settings.tts_provider,
                        emotion_plan=emotion_plan if preview_type == "full" else None,
                        synthesis_fingerprint=(
                            full_synthesis_fingerprint
                            if preview_type == "full"
                            else sample_synthesis_fingerprint
                        ),
                        voxcpm_reference_audio=(
                            _get_voxcpm_reference_audio()
                            if selected_tts_server == "voxcpm"
                            else None
                        ),
                        voxcpm_prompt_audio=(
                            _get_voxcpm_effective_prompt_audio()
                            if selected_tts_server == "voxcpm"
                            else None
                        ),
                        voxcpm_prompt_text=(
                            _get_voxcpm_prompt_text()
                            if selected_tts_server == "voxcpm"
                            else ""
                        ),
                    )
            except Exception as exc:
                logger.exception(f"failed to generate {preview_type} voice preview")
                st.error(tr("Voice Preview Failed").format(error=str(exc)))
            else:
                if preview_result and preview_result.get("busy"):
                    st.warning(tr("Voice Preview Busy"))
                elif preview_result:
                    preview_result["fingerprint"] = requested_fingerprint
                    st.session_state["voice_preview_audio"] = preview_result
                    if (
                        preview_type == "full"
                        and params.video_source == "loomloom"
                        and isinstance(preview_result.get("duration"), (int, float))
                        and math.isfinite(preview_result["duration"])
                        and preview_result["duration"] > 0
                    ):
                        # 视频设置先于音频设置渲染。完整试听成功后触发一次 rerun，
                        # 让上方素材数立刻按真实旁白时长重新推荐并刷新覆盖提示。
                        st.session_state["loomloom_video_scene_autofill_digest"] = (
                            preview_result["content_digest"]
                        )
                        st.rerun()
                else:
                    st.error(tr("Voice Preview No Audio"))

    cached_preview = st.session_state.get("voice_preview_audio")
    valid_fingerprints = {sample_fingerprint, full_fingerprint}
    if (
        cached_preview
        and cached_preview.get("fingerprint") in valid_fingerprints
        and cached_preview.get("audio_bytes")
    ):
        # 只在用户本次明确点击“试听音色”时自动播放。Streamlit 的其它控件
        # 也会触发页面 rerun；如果对缓存音频永久开启 autoplay，修改任意设置
        # 都可能让旧试听从头播放。完整试听继续保留手动播放，避免较长音频在
        # 生成完成后意外打断用户。
        should_autoplay = bool(
            short_preview_requested
            and cached_preview.get("preview_type") == "sample"
            and cached_preview.get("fingerprint") == sample_fingerprint
        )
        st.audio(
            cached_preview["audio_bytes"],
            format=cached_preview.get("mime_type", "audio/mp3"),
            autoplay=should_autoplay,
        )
        if cached_preview.get("preview_type") == "full":
            duration = cached_preview.get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                st.caption(
                    tr("Actual Voiceover Duration").format(duration=f"{duration:.1f}")
                )
            else:
                st.warning(tr("Voice Preview Duration Unavailable"))


def _get_reusable_full_voice_preview(params, voice_mode: str) -> dict | None:
    """
    返回与当前生成参数完全匹配的完整试听缓存。

    只复用完整文案试听，短音色样例永远不能进入正式任务。指纹统一覆盖文案、
    Provider、音色、语速、音量和非敏感配置摘要；任何参数变化都会自然回退到
    正常 TTS 流程。字幕时间轴和有效时长同样是必需条件，避免只复用音频后让
    Edge 字幕链路失去 SubMaker。
    """
    if voice_mode != VOICE_MODE_TTS:
        return None

    script_content = hindi_script.build_representations(
        str(params.video_script or "").strip(), params.video_language,
        pronunciation_overrides=params.pronunciation_overrides,
    ).spoken_script
    selected_tts_server = config.ui.get("tts_server", "azure-tts-v1")
    params.tts_provider = selected_tts_server
    settings = generation_settings_service.resolve_task_settings(params)
    delivery_plan = tm.emotion_storyboard.build(
        script_content,
        narrator_preset=settings.narrator_preset,
        profanity_level=settings.profanity_level,
    )
    audio_identity = generation_settings_service.synthesis_fingerprint(
        script_content, settings, delivery_plan
    )
    if (
        not script_content
        or not params.voice_name
        # 正式视频会在 MoviePy 合成阶段统一应用配音音量；部分 Provider 又会
        # 在 TTS 阶段直接写入音量增益。非默认音量下复用试听可能造成二次增益，
        # 因此先保守回退原流程，避免为少量场景引入 Provider 特判。
        or not math.isclose(float(params.voice_volume), 1.0)
    ):
        return None

    expected_fingerprint = _voice_preview_fingerprint(
        preview_type="full",
        content=script_content,
        tts_server=selected_tts_server,
        voice_name=params.voice_name,
        voice_rate=params.voice_rate,
        voice_volume=params.voice_volume,
        provider_signature=_get_voice_preview_provider_signature(selected_tts_server),
        synthesis_fingerprint=audio_identity,
    )
    cached_preview = st.session_state.get("voice_preview_audio")
    if (
        not cached_preview
        or cached_preview.get("fingerprint") != expected_fingerprint
        or cached_preview.get("preview_type") != "full"
        or not cached_preview.get("audio_bytes")
        or cached_preview.get("sub_maker") is None
    ):
        return None

    duration = cached_preview.get("duration")
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        return None

    return {
        "audio_bytes": bytes(cached_preview["audio_bytes"]),
        "duration": float(duration),
        "sub_maker": cached_preview["sub_maker"],
        "script": script_content,
        "voice_name": params.voice_name,
        "voice_rate": float(params.voice_rate),
        "voice_volume": float(params.voice_volume),
        "synthesis_fingerprint": audio_identity,
    }


def _sync_minimax_tts_api_key_input():
    """
    同步 MiniMax TTS 密码控件，并返回当前有效 Key。

    TTS 专用 Key 为空时允许复用 MiniMax LLM Key。共享 Key 只用于当前控件和
    请求，不自动复制到 [minimax_tts]，避免同一凭证在配置文件中重复维护。
    """
    widget_key = "minimax_tts_api_key_input"
    configured_key = str(config.minimax_tts.get("api_key", "") or "").strip()
    shared_key = str(
        config.app.get("minimax_api_key", "") or os.getenv("MINIMAX_API_KEY", "") or ""
    ).strip()
    effective_key = configured_key or shared_key
    had_widget_state = widget_key in st.session_state
    entered_key = str(st.session_state.get(widget_key, "") or "").strip()

    if not entered_key and effective_key:
        # 浏览器重连可能重放空密码状态。恢复已配置凭证，防止空值覆盖配置，
        # 同时确保当前 rerun 的试听请求可以直接使用有效 Key。
        st.session_state[widget_key] = effective_key
        entered_key = effective_key
        if had_widget_state:
            logger.debug("restored MiniMax TTS API key after empty session replay")
    elif not had_widget_state:
        st.session_state[widget_key] = effective_key
        entered_key = effective_key

    if entered_key and entered_key != effective_key:
        _set_runtime_config("minimax_tts", "api_key", entered_key)

    return entered_key


def _get_cached_minimax_voices(api_key: str, endpoint: str) -> list[dict[str, str]]:
    """按站点和凭证摘要读取当前会话中的 MiniMax 音色查询结果。"""
    cache = st.session_state.get("minimax_tts_voice_catalog_cache", {})
    cache_key = f"{endpoint}|{_credential_signature(api_key)}"
    cached_voices = cache.get(cache_key, [])
    return cached_voices if isinstance(cached_voices, list) else []


def _cache_minimax_voices(
    api_key: str,
    endpoint: str,
    voices: list[dict[str, str]],
):
    """缓存主动查询到的音色，避免普通控件 rerun 后重复请求 MiniMax。"""
    cache = st.session_state.setdefault("minimax_tts_voice_catalog_cache", {})
    cache_key = f"{endpoint}|{_credential_signature(api_key)}"
    cache[cache_key] = voices


def _render_minimax_tts_settings() -> tuple[list[str], dict[str, str]]:
    """渲染 MiniMax TTS 配置，并返回统一音色选择器使用的选项和文案。"""
    effective_api_key = _sync_minimax_tts_api_key_input()
    effective_api_key = st.text_input(
        tr("MiniMax TTS API Key"),
        type="password",
        key="minimax_tts_api_key_input",
    ).strip()

    dedicated_key = str(config.minimax_tts.get("api_key", "") or "").strip()
    minimax_tts_endpoints = [voice.MINIMAX_TTS_GLOBAL_URL, voice.MINIMAX_TTS_CN_URL]
    effective_endpoint = voice.get_minimax_tts_endpoint()
    if effective_endpoint not in minimax_tts_endpoints:
        effective_endpoint = voice.MINIMAX_TTS_GLOBAL_URL
    minimax_tts_base_url = stable_selectbox(
        tr("MiniMax TTS Endpoint"),
        options=minimax_tts_endpoints,
        default_value=effective_endpoint,
        key="minimax_tts_endpoint_select",
        # 复用 LLM Key 时必须跟随 LLM 所在区域，避免界面允许选择一个实际
        # 不会生效的地址；填写独立 TTS Key 后即可单独选择站点。
        disabled=not dedicated_key,
    )
    if dedicated_key:
        _set_runtime_config("minimax_tts", "base_url", minimax_tts_base_url)

    configured_model = config.minimax_tts.get(
        "model_id", voice.MINIMAX_TTS_DEFAULT_MODEL
    )
    if configured_model not in voice.MINIMAX_TTS_MODELS:
        configured_model = voice.MINIMAX_TTS_DEFAULT_MODEL
    minimax_tts_model = stable_selectbox(
        tr("MiniMax TTS Model"),
        options=list(voice.MINIMAX_TTS_MODELS),
        default_value=configured_model,
        key="minimax_tts_model_select",
    )
    _set_runtime_config("minimax_tts", "model_id", minimax_tts_model)

    if st.button(
        tr("Load MiniMax Voices"),
        key="load_minimax_voices_button",
        icon=":material/refresh:",
        width="stretch",
    ):
        try:
            available_voices = voice.get_minimax_voice_catalog(
                api_key=effective_api_key,
                endpoint=minimax_tts_base_url,
                voice_type="all",
            )
        except Exception as exc:
            # 这里必须把异常暴露给用户并记录日志。账号区域不匹配、Key 权限不足
            # 或网络失败都很常见，静默返回空列表会让用户误以为账号没有音色。
            logger.warning(f"load MiniMax voices failed: {exc}")
            st.error(tr("MiniMax Voices Load Failed").format(error=str(exc)))
        else:
            _cache_minimax_voices(
                effective_api_key,
                minimax_tts_base_url,
                available_voices,
            )
            st.success(tr("MiniMax Voices Loaded").format(count=len(available_voices)))

    available_voices = _get_cached_minimax_voices(
        effective_api_key,
        minimax_tts_base_url,
    )
    voice_labels = {
        f"minimax:{item['voice_id']}": (
            f"{item['voice_name']} ({item['voice_id']})"
            if item["voice_name"] != item["voice_id"]
            else item["voice_id"]
        )
        for item in available_voices
    }
    configured_voice_id = str(
        config.minimax_tts.get("voice_id", voice.MINIMAX_TTS_DEFAULT_VOICE)
        or voice.MINIMAX_TTS_DEFAULT_VOICE
    ).strip()
    configured_voice = f"minimax:{configured_voice_id}"
    # 尚未点击获取音色、接口暂时不可用或配置使用列表外克隆音色时，仍保留
    # 当前 Voice ID，确保原有生成流程不依赖远端音色查询结果。
    voice_labels.setdefault(configured_voice, configured_voice_id)
    return list(voice_labels), voice_labels


def _sync_elevenlabs_api_key_input():
    """
    同步 ElevenLabs 密码控件、持久化配置和环境变量，并返回当前有效 Key。

    Streamlit 在浏览器标签页连接到重启后的服务时，可能重放一个空的密码控件
    状态。这个空值无法与用户主动清空可靠区分，因此当配置文件或环境变量仍有
    Key 时，优先恢复有效值，防止空状态覆盖配置并确保本次 rerun 能立即加载
    音色。需要彻底删除 Key 时应修改配置文件或环境变量，避免重连误判。
    """
    widget_key = "elevenlabs_api_key_input"
    configured_key = str(config.elevenlabs.get("api_key", "") or "").strip()
    env_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    effective_key = configured_key or env_key
    had_widget_state = widget_key in st.session_state
    entered_key = str(st.session_state.get(widget_key, "") or "").strip()

    if not entered_key and effective_key:
        # 重连后的空状态不能覆盖有效凭证，同时必须在渲染音色列表之前恢复，
        # 否则配置文件虽然没有被清空，当前页面仍会使用空 Key 请求 ElevenLabs。
        st.session_state[widget_key] = effective_key
        entered_key = effective_key
        if had_widget_state:
            logger.debug("restored ElevenLabs API key after empty session replay")
    elif not had_widget_state:
        # 先初始化再创建控件，避免同时传 value 和 session_state 触发 Streamlit
        # 的默认值冲突警告；没有任何 Key 时初始化为空即可。
        st.session_state[widget_key] = entered_key

    if entered_key and entered_key != effective_key:
        # 用户主动输入的新值才落入 config.toml。环境变量作为有效值回填时不会
        # 被复制到文件，容器或部署平台注入的密钥仍只保留在运行环境中。
        for cache_key in list(st.session_state.keys()):
            if str(cache_key).startswith("elevenlabs_voices_"):
                del st.session_state[cache_key]
        _set_runtime_config("elevenlabs", "api_key", entered_key)

    return entered_key


def _sync_voxcpm_api_key_input():
    """恢复 VoxCPM 密码控件在重连时被 Streamlit 重放的空状态。"""
    widget_key = "voxcpm_api_key_input"
    configured_key = str(config.voxcpm.get("api_key", "") or "").strip()
    had_widget_state = widget_key in st.session_state
    entered_key = str(st.session_state.get(widget_key, "") or "").strip()

    if not entered_key and configured_key:
        # 浏览器重连可能重放空密码状态。保留已保存凭据，避免本次 rerun
        # 通过 _set_runtime_config 把 config.toml 中的有效 Key 覆盖为空。
        st.session_state[widget_key] = configured_key
        entered_key = configured_key
        if had_widget_state:
            logger.debug("restored VoxCPM API key after empty session replay")
    elif not had_widget_state:
        st.session_state[widget_key] = entered_key

    return entered_key


def _render_elevenlabs_api_key_input(label_key):
    """
    渲染 ElevenLabs TTS 与配乐共用的唯一 API Key 输入状态。

    同一页面若为 TTS 和配乐分别使用两个 widget key，Streamlit 会各自保留旧值，
    后渲染的输入框还会覆盖共享配置。这里统一使用一个 key，并集中处理环境变量
    回填、配置更新和音色缓存失效，确保界面显示与后台任务始终读取同一个值。
    """
    _sync_elevenlabs_api_key_input()
    return st.text_input(
        tr(label_key),
        type="password",
        key="elevenlabs_api_key_input",
    ).strip()


def _render_background_music_settings(params, elevenlabs_api_key_rendered=False):
    """渲染背景音乐来源与音量设置，并返回本次待保存的上传文件。"""
    uploaded_bgm_file = None
    previous_bgm_type = st.session_state.get("last_rendered_bgm_type")
    st.divider()
    bgm_options = [
        (tr("No Background Music"), ""),
        (tr("Random Background Music"), "random"),
        (tr("Preset Song"), "preset"),
        (tr("Custom Background Music"), "custom"),
        (tr("Sonilo Background Music"), "sonilo"),
        (tr("ElevenLabs Background Music"), "elevenlabs"),
    ]
    selected_bgm_type = stable_selectbox(
        tr("Background Music Source"),
        options=[value for _, value in bgm_options],
        default_value=_saved_ui_choice(
            "bgm_type",
            [value for _, value in bgm_options],
            "random",
        ),
        key="bgm_type_select",
        format_func=lambda value: dict((v, label) for label, v in bgm_options)[value],
    )
    params.bgm_type = selected_bgm_type
    _set_runtime_config("ui", "bgm_type", params.bgm_type)
    if params.bgm_type == "sonilo":
        configured_key = str(config.app.get("sonilo_api_key", "") or "").strip()
        effective_key = configured_key or os.getenv("SONILO_API_KEY", "").strip()
        entered_key = st.text_input(
            tr("Sonilo API Key"),
            value=effective_key,
            type="password",
            key="sonilo_api_key_input",
        ).strip()
        # 用户要求已配置的 Key 直接回填到密码输入框。配置值优先于环境变量；
        # 仅当用户确实修改输入或本来就使用配置时写回，避免把环境变量中的 Key
        # 在无操作的情况下复制进 config.toml。
        if configured_key or entered_key != effective_key:
            _set_runtime_config("app", "sonilo_api_key", entered_key)
    elif params.bgm_type == "elevenlabs":
        if elevenlabs_api_key_rendered:
            # TTS 区域已经渲染共享输入框时不再创建第二个 widget，避免两个独立
            # session_state 值互相覆盖。说明文字帮助用户定位上方的共用配置。
            st.caption(tr("ElevenLabs API Key Help"))
        else:
            _render_elevenlabs_api_key_input("ElevenLabs Music API Key")

    bgm_volume_options = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    params.bgm_volume = stable_selectbox(
        tr("Background Music Volume"),
        options=bgm_volume_options,
        default_value=_saved_ui_choice("bgm_volume", bgm_volume_options, 0.2),
        key="bgm_volume_select",
        format_func=lambda value: f"{int(value * 100)}%",
        disabled=not params.bgm_type,
    )
    _set_runtime_config("ui", "bgm_volume", params.bgm_volume)
    bgm_enabled = bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)

    if params.bgm_type == "custom":
        uploaded_bgm_file = st.file_uploader(
            tr("Upload Background Music"),
            type=[
                extension.removeprefix(".")
                for extension in bgm_service.SUPPORTED_BGM_EXTENSIONS
            ],
            accept_multiple_files=False,
            key="custom_bgm_uploader",
            help=tr("Upload Background Music Help"),
            # Streamlit 默认会在控件上展示全局 200MB 上限。这里必须与服务层
            # 30MB 硬限制保持一致，避免界面允许选择、提交时才被服务端拒绝。
            max_upload_size=bgm_service.MAX_BGM_UPLOAD_BYTES // (1024 * 1024),
        )
        if uploaded_bgm_file is not None and bgm_enabled:
            try:
                safe_name = bgm_service.sanitize_upload_filename(uploaded_bgm_file.name)
                # Streamlit 在调整音量等任意控件后都会重新执行页面。使用内容哈希
                # 区分上传文件，并在当前会话内缓存完整解码结果，既不能只凭同名、
                # 同大小文件误用旧结果，也避免每次 rerun 都重复调用 FFmpeg。
                validation_key = (
                    safe_name,
                    uploaded_bgm_file.size,
                    hashlib.sha256(uploaded_bgm_file.getbuffer()).hexdigest(),
                )
                cached_validation = st.session_state.get("custom_bgm_validation")
                if (
                    not cached_validation
                    or cached_validation.get("key") != validation_key
                ):
                    try:
                        bgm_service.validate_bgm_upload(
                            uploaded_bgm_file.name, uploaded_bgm_file
                        )
                    except bgm_service.BgmUploadError as exc:
                        cached_validation = {
                            "key": validation_key,
                            "error": str(exc),
                            "error_type": "upload",
                        }
                        # 同一个文件指纹的失败结果会进入会话缓存，因此这里只在
                        # 首次真实执行校验时记录一次，避免普通控件 rerun 刷屏。
                        logger.warning(
                            "WebUI background music validation rejected: "
                            f"name={safe_name}, error={str(exc)}"
                        )
                    except bgm_service.BgmServiceError as exc:
                        cached_validation = {
                            "key": validation_key,
                            "error": str(exc),
                            "error_type": "service",
                        }
                        logger.error(
                            "WebUI background music validation failed: "
                            f"name={safe_name}, error={str(exc)}"
                        )
                    else:
                        cached_validation = {
                            "key": validation_key,
                            "error": "",
                            "error_type": "",
                        }
                    st.session_state["custom_bgm_validation"] = cached_validation

                if cached_validation.get("error"):
                    if cached_validation.get("error_type") == "service":
                        raise bgm_service.BgmServiceError(cached_validation["error"])
                    raise bgm_service.BgmUploadError(cached_validation["error"])
            except bgm_service.BgmUploadError:
                # 非法文件不能沿用上一次有效上传的名称，否则任务参数可能仍指向
                # 历史 BGM。保留 UploadedFile 返回值，让用户点击生成时仍会被最终
                # 服务端校验拦截，而不是静默生成一条没有背景音乐的视频。
                params.bgm_file = ""
                st.error(tr("Invalid Background Music"))
            except bgm_service.BgmServiceError:
                params.bgm_file = ""
                st.error(tr("Background Music Validation Failed"))
            else:
                # 完整解码校验通过后才展示播放器和“已就绪”。文件仍只在点击
                # 生成时持久化，用户仅预览或随后移除文件不会污染 storage/bgm。
                uploaded_mime_type = str(getattr(uploaded_bgm_file, "type", "") or "")
                preview_mime_type = (
                    uploaded_mime_type
                    if uploaded_mime_type.startswith("audio/")
                    else mimetypes.guess_type(safe_name)[0] or "audio/mpeg"
                )
                st.audio(uploaded_bgm_file, format=preview_mime_type)
                st.info(f"{tr('Background Music Ready')}: {safe_name}")
                params.bgm_file = safe_name

        # Streamlit 会在条件控件暂时不渲染时清理其 widget state。
        # 从其它 BGM 来源切回时用已持久化值恢复；同一来源下
        # 用户主动清空时 previous_bgm_type 不变，因此不会被旧值反弹。
        if previous_bgm_type != "custom":
            st.session_state["custom_bgm_file_input"] = _saved_ui_text(
                "custom_bgm_file"
            )
        custom_bgm_file = st.text_input(
            tr("Custom Background Music File"),
            key="custom_bgm_file_input",
            disabled=uploaded_bgm_file is not None,
        )
        _set_runtime_config(
            "ui", "custom_bgm_file", custom_bgm_file.strip()
        )
        if uploaded_bgm_file is None and custom_bgm_file and bgm_enabled:
            # 文件名由服务层映射到 storage/bgm 或 resource/songs 后校验，
            # UI 不接受两个白名单目录之外的任意路径。
            params.bgm_file = custom_bgm_file.strip()
        elif not bgm_enabled:
            # 上传控件继续保留用户已选择的文件，调高音量后的下一次 rerun 会自动
            # 完整校验；当前任务参数必须清空，避免 0 音量任务保存或解析该文件。
            params.bgm_file = ""

    if params.bgm_type == "preset":
        # 服务层已经统一完成扩展名、临时文件和符号链接校验。这里直接复用其
        # 结果，避免 UI 维护第二套枚举规则，后续新增格式时也不会出现差异。
        available_song_paths = bgm_service.list_builtin_bgm_files()
        songs_by_name = {
            os.path.basename(song_path): song_path for song_path in available_song_paths
        }
        available_songs = list(songs_by_name)
        if not available_songs:
            st.warning(tr("No Background Music Available"))
            params.bgm_file = ""
        else:
            default_preset_song = _saved_ui_text("preset_song", available_songs[0])
            requested_preset_song = st.session_state.get(
                localized_widget_key("preset_song_select"), default_preset_song
            )
            if requested_preset_song not in available_songs:
                # 历史任务或其它版本导出的设置可能引用当前安装中不存在的歌曲。
                # 明确提示后由 stable_selectbox 回退第一首，避免静默换歌。
                st.warning(tr("Selected Background Music Unavailable"))
            selected_song = stable_selectbox(
                tr("Preset Song"),
                options=available_songs,
                default_value=(
                    default_preset_song
                    if default_preset_song in available_songs
                    else available_songs[0]
                ),
                key="preset_song_select",
            )
            _set_runtime_config("ui", "preset_song", selected_song)
            # 用户选择歌曲后立即提供在线试听。播放器读取的是刚刚通过服务层
            # 白名单校验得到的真实路径，不接受页面输入的任意文件路径。
            selected_song_path = songs_by_name[selected_song]
            preview_mime_type = (
                mimetypes.guess_type(selected_song_path)[0] or "audio/mpeg"
            )
            preview_available = True
            try:
                # Streamlit 读取路径失败时会把 OSError 包装成内部异常，导致下面
                # 无法按文件错误处理。先自行读取字节，既保持播放器行为，也让
                # Docker 挂载短暂失效、权限变化等情况稳定落入可控分支。
                selected_song_bytes = Path(selected_song_path).read_bytes()
            except OSError as exc:
                preview_available = False
                # 文件可能在枚举后被其它进程删除。试听失败不能中断页面或视频
                # 参数编辑，但需要保留日志以便定位运行环境和挂载问题。
                logger.warning(
                    "failed to preview preset background music: "
                    f"name={selected_song}, error={str(exc)}"
                )
                st.warning(tr("Background Music Preview Failed"))
            else:
                st.audio(selected_song_bytes, format=preview_mime_type)
            if bgm_enabled and preview_available:
                params.bgm_file = selected_song
            else:
                params.bgm_file = ""

    if params.bgm_type == "sonilo":
        if previous_bgm_type != "sonilo":
            st.session_state["sonilo_bgm_prompt_input"] = _saved_ui_text(
                "sonilo_bgm_prompt",
                max_length=sonilo_service.MAX_PROMPT_LENGTH,
            )
        params.video_music_prompt = st.text_input(
            tr("Sonilo Music Prompt"),
            key="sonilo_bgm_prompt_input",
            max_chars=sonilo_service.MAX_PROMPT_LENGTH,
            help=tr("Sonilo Music Prompt Help"),
        ).strip()
        _set_runtime_config(
            "ui", "sonilo_bgm_prompt", params.video_music_prompt
        )
        if params.video_count > 1:
            st.warning(tr("Sonilo Multiple Videos Warning"))
        if st.button(
            tr("Test Sonilo Connection"),
            key="test_sonilo_connection_button",
            width="stretch",
        ):
            try:
                sonilo_service.test_connection()
            except sonilo_service.SoniloError as exc:
                logger.warning(f"Sonilo connection test failed: {exc}")
                st.error(tr("Sonilo Connection Test Failed").format(error=str(exc)))
            else:
                st.success(tr("Sonilo Connection Test Succeeded"))
    elif params.bgm_type == "elevenlabs":
        if previous_bgm_type != "elevenlabs":
            st.session_state["elevenlabs_music_prompt_input"] = _saved_ui_text(
                "elevenlabs_music_prompt",
                max_length=elevenlabs_music_service.MAX_PROMPT_LENGTH,
            )
        params.video_music_prompt = st.text_input(
            tr("ElevenLabs Music Prompt"),
            key="elevenlabs_music_prompt_input",
            max_chars=elevenlabs_music_service.MAX_PROMPT_LENGTH,
            help=tr("ElevenLabs Music Prompt Help"),
        ).strip()
        _set_runtime_config(
            "ui", "elevenlabs_music_prompt", params.video_music_prompt
        )
        if params.video_count > 1:
            st.warning(tr("ElevenLabs Multiple Videos Warning"))
        if st.button(
            tr("Test ElevenLabs Connection"),
            key="test_elevenlabs_music_connection_button",
            width="stretch",
        ):
            try:
                elevenlabs_music_service.test_connection()
            except elevenlabs_music_service.ElevenLabsPaidPlanRequiredError:
                st.error(tr("ElevenLabs Paid Plan Required"))
            except elevenlabs_music_service.ElevenLabsMusicError as exc:
                logger.warning(f"ElevenLabs connection test failed: {exc}")
                st.error(tr("ElevenLabs Connection Test Failed").format(error=str(exc)))
            else:
                st.success(tr("ElevenLabs Connection Test Succeeded"))
    if params.bgm_type == "sonilo" and bgm_enabled and not sonilo_service.is_enabled():
        # 音量为 0 时任务层不会生成或混合 Sonilo 配乐，因此无需提示 Key；
        # 该判断与任务入口共用服务层规则，避免界面提示和实际执行条件分叉。
        st.warning(tr("Sonilo API Key Required"))
    elif (
        params.bgm_type == "elevenlabs"
        and bgm_enabled
        and not elevenlabs_music_service.is_enabled()
    ):
        st.warning(tr("ElevenLabs API Key Required"))
    st.session_state["last_rendered_bgm_type"] = params.bgm_type
    return uploaded_bgm_file


def _render_audio_settings(panel, params):
    """渲染音频设置并返回上传音频与当前配音模式。"""
    with panel:
        with st.container(border=True):
            st.write(tr("Audio Settings"))

            # 配音方式是音频设置的一级状态，负责明确区分自动配音、用户上传和无配音。
            # 旧配置没有 voice_mode 时，根据原 tts_server 的无配音哨兵保持兼容。
            saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
            saved_voice_mode = config.ui.get("voice_mode")
            if saved_voice_mode not in {
                VOICE_MODE_TTS,
                VOICE_MODE_UPLOAD,
                VOICE_MODE_NONE,
            }:
                saved_voice_mode = (
                    VOICE_MODE_NONE
                    if saved_tts_server == voice.NO_VOICE_NAME
                    else VOICE_MODE_TTS
                )
            voice_mode_options = [VOICE_MODE_TTS, VOICE_MODE_UPLOAD, VOICE_MODE_NONE]
            voice_mode_labels = {
                VOICE_MODE_TTS: tr("Automatic Voiceover"),
                VOICE_MODE_UPLOAD: tr("Upload Voiceover"),
                VOICE_MODE_NONE: tr("No Voiceover"),
            }
            voice_mode = stable_segmented_control(
                tr("Voiceover Mode"),
                options=voice_mode_options,
                default_value=saved_voice_mode,
                key="voice_mode_control",
                format_func=lambda value: voice_mode_labels[value],
                width="stretch",
            )
            _set_runtime_config("ui", "voice_mode", voice_mode)
            tts_mode_enabled = voice_mode == VOICE_MODE_TTS

            # Provider 下拉只负责选择自动配音服务；无配音已经由上方模式控制，
            # 不再作为 TTS Provider 混入列表，避免两个入口表达同一状态。
            tts_servers = [
                ("azure-tts-v1", "Azure TTS V1 (Edge TTS)"),
                ("azure-tts-v2", "Azure TTS V2"),
                ("siliconflow", "SiliconFlow TTS"),
                ("gemini-tts", "Google Gemini TTS"),
                ("mimo-tts", "Xiaomi MiMo TTS"),
                ("minimax-tts", "MiniMax TTS"),
                ("elevenlabs", "ElevenLabs TTS"),
                ("chatterbox", "Chatterbox TTS"),
                ("kokoro", "Kokoro TTS"),
                ("fish_audio", "Fish Audio TTS"),
                ("voxcpm", "VoxCPM TTS"),
            ]

            tts_server_values = [server_value for server_value, _ in tts_servers]
            if saved_tts_server not in tts_server_values:
                saved_tts_server = "azure-tts-v1"

            if tts_mode_enabled:
                selected_tts_server = stable_selectbox(
                    tr("Voiceover Service"),
                    options=tts_server_values,
                    default_value=saved_tts_server,
                    key="tts_server_select",
                    format_func=lambda value: dict(
                        (v, label) for v, label in tts_servers
                    )[value],
                )
            else:
                # 非自动配音模式不渲染 TTS 控件，但保留上次选择，切回后可以继续使用。
                selected_tts_server = saved_tts_server

            _set_runtime_config("ui", "tts_server", selected_tts_server)
            params.tts_provider = selected_tts_server
            params.tts_model = config.ui.get(
                "gemini_tts_model", params.tts_model
            )

            # 服务说明紧跟 Provider 选择，先告诉用户需要准备什么，再进入音色和
            # 凭证配置。没有说明的 Provider 不渲染空提示块。
            if tts_mode_enabled:
                provider_tips = get_tts_provider_tips(selected_tts_server)
                if provider_tips:
                    st.info(provider_tips)

            # MiniMax 只复用下方通用“配音声音”选择器。Provider 配置函数负责
            # 刷新远端音色并返回友好文案，不再额外渲染 Voice ID 和音色下拉框。
            minimax_voices = []
            minimax_voice_labels = {}
            if tts_mode_enabled and selected_tts_server == "minimax-tts":
                minimax_voices, minimax_voice_labels = _render_minimax_tts_settings()

            # 根据选择的TTS服务器获取声音列表
            filtered_voices = []
            saved_voice_name = config.ui.get("voice_name", "")
            elevenlabs_api_key_rendered = False

            if not tts_mode_enabled:
                # 上传音频和无配音模式不加载远程音色，减少无意义的网络请求和界面噪音。
                filtered_voices = []
            elif selected_tts_server == "siliconflow":
                # 获取硅基流动的声音列表
                filtered_voices = voice.get_siliconflow_voices()
            elif selected_tts_server == "gemini-tts":
                # 获取Gemini TTS的声音列表
                filtered_voices = voice.get_gemini_voices()
            elif selected_tts_server == "mimo-tts":
                # 获取 Xiaomi MiMo TTS 的预置音色列表
                filtered_voices = voice.get_mimo_voices()
            elif selected_tts_server == "minimax-tts":
                filtered_voices = minimax_voices
            elif selected_tts_server == "elevenlabs":
                # 音色列表位于 Key 输入框之前渲染，必须先统一恢复重连状态并读取
                # 配置/环境变量，否则页面会用空 Key 加载并缓存空音色列表。
                saved_elevenlabs_api_key = _sync_elevenlabs_api_key_input()
                cache_key = f"elevenlabs_voices_{saved_elevenlabs_api_key}"
                if cache_key not in st.session_state:
                    st.session_state[cache_key] = voice.get_elevenlabs_voices(
                        saved_elevenlabs_api_key
                    )
                filtered_voices = st.session_state[cache_key]
            elif selected_tts_server == "chatterbox":
                # 自托管 Chatterbox 服务的预置音色（来自 [chatterbox] voices 配置）
                _sync_chatterbox_config_from_session_state()
                filtered_voices = voice.get_chatterbox_voices()
            elif selected_tts_server == "kokoro":
                # 自托管 Kokoro 服务的音色：[kokoro] voices 为空时从服务端 /audio/voices 读取
                _sync_kokoro_config_from_session_state()
                filtered_voices = _get_kokoro_voice_options(saved_voice_name)
            elif selected_tts_server == "fish_audio":
                filtered_voices = voice.get_fish_audio_voices()
            elif selected_tts_server == "voxcpm":
                filtered_voices = voice.get_voxcpm_voices()
            else:
                # 获取Azure的声音列表
                all_voices = voice.get_all_azure_voices(filter_locals=None)

                # 根据选择的TTS服务器筛选声音
                for v in all_voices:
                    if selected_tts_server == "azure-tts-v2":
                        # V2版本的声音名称中包含"v2"
                        if "V2" in v:
                            filtered_voices.append(v)
                    else:
                        # V1版本的声音名称中不包含"v2"
                        if "V2" not in v:
                            filtered_voices.append(v)

            def _friendly(v):
                if voice.is_no_voice(v):
                    return tr("No Voice Selected")
                if voice.is_elevenlabs_voice(v):
                    parts = v.split(":", 2)
                    return parts[2] if len(parts) >= 3 else v
                if voice.is_chatterbox_voice(v) or voice.is_kokoro_voice(v):
                    name = v.split(":", 1)[1] if ":" in v else v
                    return name.replace("-Female", "").replace("-Male", "")
                if voice.is_minimax_voice(v):
                    return minimax_voice_labels.get(v, v.split(":", 1)[1])
                if voice.is_fish_audio_voice(v):
                    parts = v.split(":", 2)
                    display_name = parts[2] if len(parts) >= 3 else v
                    return (
                        display_name.replace("Female", tr("Female"))
                        .replace("Male", tr("Male"))
                    )
                if voice.is_voxcpm_voice(v):
                    return v.split(":", 1)[1] or DEFAULT_VOXCPM_VOICE
                return (
                    v.replace("Female", tr("Female"))
                    .replace("Male", tr("Male"))
                    .replace("Neural", "")
                )

            friendly_names = {v: _friendly(v) for v in filtered_voices}

            # Gemini 旧目录把推测的性别放在值里（例如 Charon-Male）。按基础
            # voice name 映射到新的官方风格值，升级后继续保留用户原来的音色。
            if (
                selected_tts_server == "gemini-tts"
                and saved_voice_name not in friendly_names
            ):
                saved_gemini_voice = voice.parse_gemini_voice_name(saved_voice_name)
                saved_voice_name = next(
                    (
                        candidate
                        for candidate in filtered_voices
                        if voice.parse_gemini_voice_name(candidate)
                        == saved_gemini_voice
                    ),
                    saved_voice_name,
                )

            saved_voice_name_index = 0

            # 检查保存的声音是否在当前筛选的声音列表中
            if saved_voice_name in friendly_names:
                saved_voice_name_index = list(friendly_names.keys()).index(
                    saved_voice_name
                )
            else:
                # 如果不在，则根据当前UI语言选择一个默认声音
                for i, v in enumerate(filtered_voices):
                    if v.lower().startswith(st.session_state["ui_language"].lower()):
                        saved_voice_name_index = i
                        break

            # 如果没有找到匹配的声音，使用第一个声音
            if saved_voice_name_index >= len(friendly_names) and friendly_names:
                saved_voice_name_index = 0

            # 确保有声音可选
            if tts_mode_enabled and friendly_names:
                voice_name = stable_selectbox(
                    tr("Voiceover Voice"),
                    options=list(friendly_names.keys()),
                    default_value=list(friendly_names.keys())[saved_voice_name_index],
                    key=f"speech_synthesis_select_{selected_tts_server}",
                    format_func=lambda value: friendly_names.get(
                        value,
                        str(value).removeprefix("minimax:"),
                    ),
                    # MiniMax 支持用户直接输入列表外的克隆或生成音色 ID；其它
                    # Provider 维持原选择器行为，不扩大本次修改的影响范围。
                    accept_new_options=selected_tts_server == "minimax-tts",
                )

                if selected_tts_server == "minimax-tts":
                    custom_voice_id = str(voice_name or "").strip()
                    if custom_voice_id and not voice.is_minimax_voice(custom_voice_id):
                        voice_name = f"minimax:{custom_voice_id}"
                    if voice.is_minimax_voice(voice_name):
                        _set_runtime_config(
                            "minimax_tts",
                            "voice_id",
                            voice_name.split(":", 1)[1],
                        )

                params.voice_name = voice_name
                if not voice.is_no_voice(voice_name):
                    # 占位 sentinel 仅用于非自动模式的禁用展示，不覆盖用户上一次
                    # 真正选择的音色，切回自动配音后可以恢复原设置。
                    _set_runtime_config("ui", "voice_name", voice_name)
            elif tts_mode_enabled:
                # 如果没有声音可选，显示提示信息
                st.warning(
                    tr(
                        "No voices available for the selected TTS server. Please select another server."
                    )
                )
                voice_name = ""
                params.voice_name = ""
                _set_runtime_config("ui", "voice_name", "")
            else:
                # 非自动配音模式不显示音色控件，只复用保存值维持参数结构稳定。
                voice_name = saved_voice_name or voice.NO_VOICE_NAME
                params.voice_name = voice_name

            # 当选择V2版本或者声音是V2声音时，显示服务区域和API key输入框
            if tts_mode_enabled and (
                selected_tts_server == "azure-tts-v2"
                or (voice_name and voice.is_azure_v2_voice(voice_name))
            ):
                saved_azure_speech_region = config.azure.get("speech_region", "")
                saved_azure_speech_key = config.azure.get("speech_key", "")
                azure_speech_region = st.text_input(
                    tr("Speech Region"),
                    value=saved_azure_speech_region,
                    key="azure_speech_region_input",
                )
                azure_speech_key = st.text_input(
                    tr("Speech Key"),
                    value=saved_azure_speech_key,
                    type="password",
                    key="azure_speech_key_input",
                )
                _set_runtime_config("azure", "speech_region", azure_speech_region)
                _set_runtime_config("azure", "speech_key", azure_speech_key)

            if tts_mode_enabled and selected_tts_server == "gemini-tts":
                # Gemini TTS 与 Gemini LLM 共用同一份密钥；在音频面板提供直接入口，
                # 用户无需先切换 LLM Provider 才能完成语音配置。
                gemini_tts_api_key = st.text_input(
                    tr("Gemini API Key"),
                    value=config.app.get("gemini_api_key", ""),
                    type="password",
                    key="gemini_tts_api_key_input",
                )
                _set_runtime_config("app", "gemini_api_key", gemini_tts_api_key)

            # 当选择硅基流动时，显示API key输入框和说明信息
            if tts_mode_enabled and (
                selected_tts_server == "siliconflow"
                or (voice_name and voice.is_siliconflow_voice(voice_name))
            ):
                saved_siliconflow_api_key = config.siliconflow.get("api_key", "")

                siliconflow_api_key = st.text_input(
                    tr("SiliconFlow API Key"),
                    value=saved_siliconflow_api_key,
                    type="password",
                    key="siliconflow_api_key_input",
                )

                _set_runtime_config("siliconflow", "api_key", siliconflow_api_key)

            # 当选择 Xiaomi MiMo TTS 时，复用 MiMo LLM provider 的 API Key。
            # 这样用户如果同时使用 MiMo 生成文案和语音，只需要维护一份密钥。
            if tts_mode_enabled and (
                selected_tts_server == "mimo-tts"
                or (voice_name and voice.is_mimo_voice(voice_name))
            ):
                saved_mimo_api_key = config.app.get("mimo_api_key", "")

                mimo_api_key = st.text_input(
                    tr("MiMo API Key"),
                    value=saved_mimo_api_key,
                    type="password",
                    key="mimo_tts_api_key_input",
                )

                _set_runtime_config("app", "mimo_api_key", mimo_api_key)

            # ElevenLabs API key section
            if tts_mode_enabled and (
                selected_tts_server == "elevenlabs"
                or (voice_name and voice.is_elevenlabs_voice(voice_name))
            ):
                _render_elevenlabs_api_key_input(
                    "ElevenLabs API Key",
                )
                elevenlabs_api_key_rendered = True

                _elevenlabs_models = [
                    "eleven_multilingual_v2",
                    "eleven_flash_v2_5",
                    "eleven_v3",
                ]
                saved_elevenlabs_model = config.elevenlabs.get(
                    "model_id", "eleven_multilingual_v2"
                )
                if saved_elevenlabs_model not in _elevenlabs_models:
                    saved_elevenlabs_model = "eleven_multilingual_v2"
                elevenlabs_model = stable_selectbox(
                    tr("ElevenLabs Model"),
                    options=_elevenlabs_models,
                    default_value=saved_elevenlabs_model,
                    key="elevenlabs_model_select",
                )
                _set_runtime_config("elevenlabs", "model_id", elevenlabs_model)

            # Fish Audio API settings section
            if tts_mode_enabled and (
                selected_tts_server == "fish_audio"
                or (voice_name and voice.is_fish_audio_voice(voice_name))
            ):
                saved_fish_api_key = (
                    config.fish_audio.get("api_key", "")
                    if hasattr(config, "fish_audio") and isinstance(config.fish_audio, dict)
                    else ""
                )
                fish_audio_api_key = st.text_input(
                    tr("Fish Audio API Key"),
                    value=saved_fish_api_key,
                    type="password",
                    key="fish_audio_api_key_input",
                )
                _set_runtime_config("fish_audio", "api_key", fish_audio_api_key)

                _fish_audio_models = [
                    "s2.1-pro-free",
                    "s2.1-pro",
                    "s2-pro",
                ]
                saved_fish_model = (
                    config.fish_audio.get("model", "s2.1-pro-free")
                    if hasattr(config, "fish_audio") and isinstance(config.fish_audio, dict)
                    else "s2.1-pro-free"
                )
                if saved_fish_model not in _fish_audio_models:
                    saved_fish_model = "s2.1-pro-free"
                fish_model = stable_selectbox(
                    tr("Fish Audio Model"),
                    options=_fish_audio_models,
                    default_value=saved_fish_model,
                    key="fish_audio_model_select",
                )
                _set_runtime_config("fish_audio", "model", fish_model)

            # ModelBest hosts VoxCPM behind its streaming Audio Speech API.
            # The fixed provider endpoint is still editable for compatible
            # gateways, while the user only has to supply an API key and a
            # speech_synthesis-capable model id for the standard platform.
            if tts_mode_enabled and (
                selected_tts_server == "voxcpm"
                or (voice_name and voice.is_voxcpm_voice(voice_name))
            ):
                _sync_voxcpm_api_key_input()
                voxcpm_api_key = st.text_input(
                    tr("VoxCPM API Key"),
                    type="password",
                    key="voxcpm_api_key_input",
                )
                _set_runtime_config("voxcpm", "api_key", voxcpm_api_key)

                voxcpm_model = st.text_input(
                    tr("VoxCPM Model ID"),
                    value=config.voxcpm.get("model_id", ""),
                    key="voxcpm_model_id_input",
                    placeholder=tr("VoxCPM Model ID Placeholder"),
                )
                _set_runtime_config("voxcpm", "model_id", voxcpm_model.strip())

                voxcpm_base_url = st.text_input(
                    tr("VoxCPM Base URL"),
                    value=config.voxcpm.get("base_url") or DEFAULT_VOXCPM_BASE_URL,
                    key="voxcpm_base_url_input",
                    placeholder=DEFAULT_VOXCPM_BASE_URL,
                )
                _set_runtime_config(
                    "voxcpm",
                    "base_url",
                    (voxcpm_base_url or DEFAULT_VOXCPM_BASE_URL).strip().rstrip("/"),
                )

                uploaded_reference_audio = st.file_uploader(
                    tr("VoxCPM Reference Audio"),
                    type=list(voice.VOXCPM_REFERENCE_AUDIO_FILE_TYPES),
                    key="voxcpm_reference_audio_uploader",
                    help=tr("VoxCPM Reference Audio Help"),
                )
                reference_audio = _sync_voxcpm_reference_audio(
                    uploaded_reference_audio
                )
                st.caption(tr("VoxCPM Reference Audio Notice"))
                reference_audio_error = st.session_state.get(
                    VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY
                )
                if reference_audio_error:
                    st.error(
                        tr("VoxCPM Reference Audio Invalid").format(
                            error=reference_audio_error
                        )
                    )

                if reference_audio:
                    high_fidelity_enabled = st.toggle(
                        tr("VoxCPM High Fidelity Delivery"),
                        key=VOXCPM_HIGH_FIDELITY_SESSION_KEY,
                        help=tr("VoxCPM High Fidelity Delivery Help"),
                    )
                    if high_fidelity_enabled:
                        use_separate_prompt_audio = st.toggle(
                            tr("VoxCPM Separate Prompt Audio"),
                            key=VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY,
                            help=tr("VoxCPM Separate Prompt Audio Help"),
                        )
                        _sync_voxcpm_prompt_example_mode(use_separate_prompt_audio)
                        if use_separate_prompt_audio:
                            prompt_audio_file = st.file_uploader(
                                tr("VoxCPM Prompt Audio"),
                                type=list(voice.VOXCPM_REFERENCE_AUDIO_FILE_TYPES),
                                key="voxcpm_prompt_audio_uploader",
                                help=tr("VoxCPM Prompt Audio Help"),
                            )
                            _sync_voxcpm_prompt_audio(prompt_audio_file)
                        else:
                            _clear_voxcpm_separate_prompt_audio()
                        effective_prompt_audio = _get_voxcpm_effective_prompt_audio()
                        if st.button(
                            tr("Transcribe VoxCPM Prompt Audio"),
                            key="transcribe_voxcpm_prompt_audio_button",
                            icon=":material/transcribe:",
                            help=tr("Transcribe VoxCPM Prompt Audio Help"),
                            width="stretch",
                            disabled=not bool(effective_prompt_audio),
                        ):
                            try:
                                with st.spinner(tr("Transcribing VoxCPM Prompt Audio")):
                                    recognized_text = subtitle.transcribe_audio_bytes(
                                        effective_prompt_audio
                                    )
                            except Exception:
                                logger.exception(
                                    "failed to transcribe VoxCPM prompt audio"
                                )
                                recognized_text = ""
                            if recognized_text:
                                st.session_state[VOXCPM_PROMPT_TEXT_SESSION_KEY] = (
                                    recognized_text
                                )
                                st.toast(tr("VoxCPM Prompt Audio Transcribed"))
                            else:
                                st.error(tr("VoxCPM Prompt Audio Transcription Failed"))
                        st.text_area(
                            tr("VoxCPM Prompt Text"),
                            key=VOXCPM_PROMPT_TEXT_SESSION_KEY,
                            help=tr("VoxCPM Prompt Text Help"),
                            height=100,
                        )
                        st.caption(tr("VoxCPM Prompt Transcript Review"))
                        prompt_error = _get_voxcpm_prompt_validation_error()
                        if prompt_error:
                            st.error(
                                tr("VoxCPM Prompt Invalid").format(
                                    error=prompt_error
                                )
                            )
                    else:
                        _clear_voxcpm_prompt_state()

            # Chatterbox API settings section (self-hosted, OpenAI-compatible)
            if tts_mode_enabled and (
                selected_tts_server == "chatterbox"
                or (voice_name and voice.is_chatterbox_voice(voice_name))
            ):
                chatterbox_base_url = st.text_input(
                    tr("Chatterbox Base URL"),
                    value=config.chatterbox.get("base_url")
                    or DEFAULT_CHATTERBOX_BASE_URL,
                    key="chatterbox_base_url_input",
                    placeholder=tr("Chatterbox Base URL Placeholder"),
                )
                _set_runtime_config(
                    "chatterbox", "base_url", (chatterbox_base_url or "").strip()
                )

                chatterbox_api_key = st.text_input(
                    tr("Chatterbox API Key"),
                    value=config.chatterbox.get("api_key", ""),
                    type="password",
                    key="chatterbox_api_key_input",
                )
                _set_runtime_config("chatterbox", "api_key", chatterbox_api_key)

                chatterbox_model = st.text_input(
                    tr("Chatterbox Model"),
                    value=config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
                    key="chatterbox_model_input",
                )
                _set_runtime_config(
                    "chatterbox",
                    "model_id",
                    (chatterbox_model or DEFAULT_CHATTERBOX_MODEL).strip(),
                )

                _saved_chatterbox_voices = (
                    _parse_chatterbox_voices(config.chatterbox.get("voices"))
                    or DEFAULT_CHATTERBOX_VOICES
                )
                if isinstance(_saved_chatterbox_voices, list):
                    _saved_chatterbox_voices = ", ".join(_saved_chatterbox_voices)
                chatterbox_voices = st.text_input(
                    tr("Chatterbox Voices"),
                    value=str(_saved_chatterbox_voices or ""),
                    key="chatterbox_voices_input",
                    placeholder=tr("Chatterbox Voices Placeholder"),
                )
                _set_runtime_config(
                    "chatterbox",
                    "voices",
                    _parse_chatterbox_voices(chatterbox_voices),
                )

            # Kokoro API settings section (self-hosted, OpenAI-compatible; voices listed from the server when left empty)
            if tts_mode_enabled and (
                selected_tts_server == "kokoro"
                or (voice_name and voice.is_kokoro_voice(voice_name))
            ):
                kokoro_base_url = st.text_input(
                    tr("Kokoro Base URL"),
                    value=config.kokoro.get("base_url")
                    or DEFAULT_KOKORO_BASE_URL,
                    key="kokoro_base_url_input",
                    placeholder=tr("Kokoro Base URL Placeholder"),
                )
                _set_runtime_config(
                    "kokoro", "base_url", (kokoro_base_url or "").strip()
                )

                kokoro_api_key = st.text_input(
                    tr("Kokoro API Key"),
                    value=config.kokoro.get("api_key", ""),
                    type="password",
                    key="kokoro_api_key_input",
                )
                _set_runtime_config("kokoro", "api_key", kokoro_api_key)

                kokoro_model = st.text_input(
                    tr("Kokoro Model"),
                    value=config.kokoro.get("model_id") or DEFAULT_KOKORO_MODEL,
                    key="kokoro_model_input",
                )
                _set_runtime_config(
                    "kokoro",
                    "model_id",
                    (kokoro_model or DEFAULT_KOKORO_MODEL).strip(),
                )

                _saved_kokoro_voices = (
                    _parse_chatterbox_voices(config.kokoro.get("voices"))
                    or DEFAULT_KOKORO_VOICES
                )
                if isinstance(_saved_kokoro_voices, list):
                    _saved_kokoro_voices = ", ".join(_saved_kokoro_voices)
                kokoro_voices = st.text_input(
                    tr("Kokoro Voices"),
                    value=str(_saved_kokoro_voices or ""),
                    key="kokoro_voices_input",
                    placeholder=tr("Kokoro Voices Placeholder"),
                )
                _set_runtime_config(
                    "kokoro",
                    "voices",
                    _parse_chatterbox_voices(kokoro_voices),
                )

            # 三种模式只渲染当前任务真正需要的控件。自动配音可调音量和语速；
            # 上传音频只需要文件和音量；无配音不再展示无效设置。
            params.voice_name = (
                voice.NO_VOICE_NAME if voice_mode == VOICE_MODE_NONE else voice_name
            )
            params.voice_volume = 1.0
            params.voice_rate = 1.0
            uploaded_audio_file = None
            voice_volume_options = [0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 5.0]
            voice_rate_options = [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0]

            if tts_mode_enabled:
                voice_control_cols = st.columns(2)
                with voice_control_cols[0]:
                    params.voice_volume = stable_selectbox(
                        tr("Voiceover Volume"),
                        options=voice_volume_options,
                        default_value=_saved_ui_choice(
                            "voice_volume", voice_volume_options, 1.0
                        ),
                        key="voice_volume_select",
                        format_func=lambda value: f"{int(value * 100)}%",
                        help=tr("Voiceover Volume Help"),
                    )

                with voice_control_cols[1]:
                    is_voxcpm = bool(
                        selected_tts_server == "voxcpm"
                        or (voice_name and voice.is_voxcpm_voice(voice_name))
                    )
                    params.voice_rate = stable_selectbox(
                        tr("Voiceover Speed"),
                        options=voice_rate_options,
                        default_value=_saved_ui_choice(
                            "voice_rate", voice_rate_options, 1.0
                        ),
                        key="voice_rate_select",
                        format_func=lambda value: f"{value:.1f}×",
                        help=(
                            tr("VoxCPM Speed Not Supported")
                            if is_voxcpm
                            else tr("Voiceover Speed Help")
                        ),
                        disabled=is_voxcpm,
                    )
                _set_runtime_config("ui", "voice_volume", params.voice_volume)
                _set_runtime_config("ui", "voice_rate", params.voice_rate)

                # 试听必须位于音量和语速控件之后，确保调用使用当前控件值。
                _render_voice_preview(
                    params,
                    friendly_names,
                    selected_tts_server,
                    voice_name,
                )
            elif voice_mode == VOICE_MODE_UPLOAD:
                custom_audio_file_types = sorted(
                    extension.removeprefix(".") for extension in CUSTOM_AUDIO_EXTENSIONS
                )
                uploaded_audio_file = st.file_uploader(
                    tr("Upload Voiceover File"),
                    type=custom_audio_file_types
                    + [file_type.upper() for file_type in custom_audio_file_types],
                    accept_multiple_files=False,
                    key="custom_audio_file_uploader",
                    help=tr("Upload Voiceover File Help"),
                )
                params.voice_volume = stable_selectbox(
                    tr("Voiceover Volume"),
                    options=voice_volume_options,
                    default_value=_saved_ui_choice(
                        "voice_volume", voice_volume_options, 1.0
                    ),
                    key="voice_volume_select",
                    format_func=lambda value: f"{int(value * 100)}%",
                    help=tr("Voiceover Volume Help"),
                )
                _set_runtime_config("ui", "voice_volume", params.voice_volume)
                if uploaded_audio_file:
                    st.audio(uploaded_audio_file, format="audio/mp3")
                    st.info(
                        tr(
                            "Custom audio will be used directly. TTS synthesis will be skipped for this task."
                        )
                    )
            uploaded_bgm_file = _render_background_music_settings(
                params,
                elevenlabs_api_key_rendered=elevenlabs_api_key_rendered,
            )
    return uploaded_audio_file, uploaded_bgm_file, voice_mode


def _render_subtitle_settings(panel, params):
    """渲染字幕设置并更新生成参数。"""
    with panel:
        with st.container(border=True):
            st.write(tr("Subtitle Settings"))
            st.session_state.setdefault(
                "subtitle_enabled_checkbox",
                _saved_ui_bool(
                    "subtitle_enabled",
                    DEFAULT_SUBTITLE_SETTINGS["subtitle_enabled"],
                ),
            )
            params.subtitle_enabled = st.checkbox(
                tr("Enable Subtitles"),
                key="subtitle_enabled_checkbox",
            )
            _set_runtime_config("ui", "subtitle_enabled", params.subtitle_enabled)
            subtitle_settings_disabled = not params.subtitle_enabled
            font_names = get_all_fonts()
            saved_font_name = config.ui.get(
                "font_name", DEFAULT_SUBTITLE_SETTINGS["font_name"]
            )
            saved_font_name_index = 0
            if saved_font_name in font_names:
                saved_font_name_index = font_names.index(saved_font_name)
            params.font_name = stable_selectbox(
                tr("Font"),
                options=font_names,
                default_value=font_names[saved_font_name_index] if font_names else "",
                key="font_name_select",
                disabled=subtitle_settings_disabled,
            )
            _set_runtime_config("ui", "font_name", params.font_name)

            subtitle_positions = [
                (tr("Top"), "top"),
                (tr("Center"), "center"),
                (tr("Bottom"), "bottom"),
                (tr("2/3 from Bottom"), "two_thirds_bottom"),
                (tr("Custom"), "custom"),
            ]
            saved_subtitle_position = config.ui.get(
                "subtitle_position", DEFAULT_SUBTITLE_SETTINGS["subtitle_position"]
            )
            saved_position_index = 2
            for i, (_, pos_value) in enumerate(subtitle_positions):
                if pos_value == saved_subtitle_position:
                    saved_position_index = i
                    break
            selected_subtitle_position = stable_selectbox(
                tr("Position"),
                options=[value for _, value in subtitle_positions],
                default_value=subtitle_positions[saved_position_index][1],
                key="subtitle_position_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in subtitle_positions
                ).get(value, value),
                disabled=subtitle_settings_disabled,
            )
            params.subtitle_position = selected_subtitle_position
            _set_runtime_config("ui", "subtitle_position", params.subtitle_position)

            # Subtitle Display Mode (Sentence vs Single Word)
            subtitle_display_modes = [
                (tr("Sentence by Sentence"), "sentence"),
                (tr("Single Word (Word by Word)"), "word_by_word"),
            ]
            saved_display_mode = config.ui.get(
                "subtitle_display_mode",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_display_mode"],
            )
            saved_mode_idx = 0
            for i, (_, mode_val) in enumerate(subtitle_display_modes):
                if mode_val == saved_display_mode:
                    saved_mode_idx = i
                    break
            selected_display_mode = stable_selectbox(
                tr("Display Mode"),
                options=[val for _, val in subtitle_display_modes],
                default_value=subtitle_display_modes[saved_mode_idx][1],
                key="subtitle_display_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in subtitle_display_modes
                ).get(value, value),
                help=tr("Word-by-word Timing Help"),
                disabled=subtitle_settings_disabled,
            )
            params.subtitle_display_mode = selected_display_mode
            _set_runtime_config(
                "ui", "subtitle_display_mode", params.subtitle_display_mode
            )

            params.hindi_alignment_policy = stable_selectbox(
                "Hindi alignment policy", options=["balanced", "strict"],
                default_value=config.ui.get("hindi_alignment_policy", "balanced"),
                key="hindi_alignment_policy_select",
                help="Balanced reports isolated marginal words. Strict rejects every score below 0.35.",
            )
            _set_runtime_config("ui", "hindi_alignment_policy", params.hindi_alignment_policy)

            # Subtitle Animation (None vs Pop Spring)
            subtitle_animations = [
                (tr("None (Animation)"), "none"),
                (tr("Pop Up (Spring)"), "pop_spring"),
            ]
            saved_anim = config.ui.get(
                "subtitle_animation",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_animation"],
            )
            saved_anim_idx = 0
            for i, (_, anim_val) in enumerate(subtitle_animations):
                if anim_val == saved_anim:
                    saved_anim_idx = i
                    break
            selected_anim = stable_selectbox(
                tr("Subtitle Animation"),
                options=[val for _, val in subtitle_animations],
                default_value=subtitle_animations[saved_anim_idx][1],
                key="subtitle_animation_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in subtitle_animations
                ).get(value, value),
                disabled=subtitle_settings_disabled,
            )
            params.subtitle_animation = selected_anim
            _set_runtime_config(
                "ui", "subtitle_animation", params.subtitle_animation
            )

            if params.subtitle_position == "custom":
                saved_custom_position = config.ui.get(
                    "custom_position", DEFAULT_SUBTITLE_SETTINGS["custom_position"]
                )
                st.session_state.setdefault(
                    "custom_position_input", str(saved_custom_position)
                )
                custom_position = st.text_input(
                    tr("Custom Position (% from top)"),
                    key="custom_position_input",
                    disabled=subtitle_settings_disabled,
                )
                try:
                    params.custom_position = float(custom_position)
                    if params.custom_position < 0 or params.custom_position > 100:
                        st.error(tr("Please enter a value between 0 and 100"))
                    else:
                        _set_runtime_config(
                            "ui", "custom_position", params.custom_position
                        )
                except ValueError:
                    st.error(tr("Please enter a valid number"))

            # 非中文语言的颜色标签通常比中文更长。为颜色选择器保留适当宽度，
            # 避免标签换行，同时仍给字号滑块保留足够的可操作空间。
            font_cols = st.columns([0.42, 0.58])
            with font_cols[0]:
                saved_text_fore_color = config.ui.get(
                    "text_fore_color", DEFAULT_SUBTITLE_SETTINGS["text_fore_color"]
                )
                st.session_state.setdefault("font_color_picker", saved_text_fore_color)
                params.text_fore_color = st.color_picker(
                    tr("Font Color"),
                    key="font_color_picker",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "text_fore_color", params.text_fore_color)

            with font_cols[1]:
                saved_font_size = config.ui.get(
                    "font_size", DEFAULT_SUBTITLE_SETTINGS["font_size"]
                )
                st.session_state.setdefault("font_size_slider", saved_font_size)
                params.font_size = st.slider(
                    tr("Font Size"),
                    30,
                    100,
                    key="font_size_slider",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "font_size", params.font_size)

            stroke_cols = st.columns([0.42, 0.58])
            with stroke_cols[0]:
                st.session_state.setdefault(
                    "stroke_color_picker",
                    _saved_ui_color(
                        "stroke_color", DEFAULT_SUBTITLE_SETTINGS["stroke_color"]
                    ),
                )
                params.stroke_color = st.color_picker(
                    tr("Stroke Color"),
                    key="stroke_color_picker",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "stroke_color", params.stroke_color)
            with stroke_cols[1]:
                st.session_state.setdefault(
                    "stroke_width_slider",
                    _saved_ui_number(
                        "stroke_width",
                        DEFAULT_SUBTITLE_SETTINGS["stroke_width"],
                        0.0,
                        10.0,
                    ),
                )
                params.stroke_width = st.slider(
                    tr("Stroke Width"),
                    0.0,
                    10.0,
                    key="stroke_width_slider",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "stroke_width", params.stroke_width)

            # 背景开关的本地化名称普遍比颜色标签更长，因此让开关占据略多空间。
            subtitle_bg_cols = st.columns([0.55, 0.45])
            saved_subtitle_background_enabled = config.ui.get(
                "subtitle_background_enabled",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_background_enabled"],
            )
            st.session_state.setdefault(
                "subtitle_background_enabled_checkbox",
                saved_subtitle_background_enabled,
            )
            with subtitle_bg_cols[0]:
                subtitle_background_enabled = st.checkbox(
                    tr("Enable Subtitle Background"),
                    key="subtitle_background_enabled_checkbox",
                    disabled=subtitle_settings_disabled,
                )
            _set_runtime_config(
                "ui",
                "subtitle_background_enabled",
                subtitle_background_enabled,
            )

            # 背景颜色和圆角样式都从属于字幕背景开关。子控件始终保留在页面中，
            # 父开关关闭时统一禁用，避免一个控件消失而另一个控件禁用造成布局跳动。
            # 颜色值仍保存在 UI 配置中，重新启用背景后可以恢复用户之前的选择；
            # 传给生成服务的参数则设为 False，确保关闭状态不会实际渲染背景。
            saved_subtitle_background_color = config.ui.get(
                "subtitle_background_color",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_background_color"],
            )
            st.session_state.setdefault(
                "subtitle_background_color_picker",
                saved_subtitle_background_color,
            )
            with subtitle_bg_cols[1]:
                selected_subtitle_background_color = st.color_picker(
                    tr("Subtitle Background Color"),
                    key="subtitle_background_color_picker",
                    disabled=subtitle_settings_disabled
                    or not subtitle_background_enabled,
                )
            _set_runtime_config(
                "ui",
                "subtitle_background_color",
                selected_subtitle_background_color,
            )
            params.text_background_color = (
                selected_subtitle_background_color
                if subtitle_background_enabled
                else False
            )

            saved_rounded_subtitle_background = config.ui.get(
                "rounded_subtitle_background",
                DEFAULT_SUBTITLE_SETTINGS["rounded_subtitle_background"],
            )
            # 背景关闭时，圆角背景没有可渲染的底色。这里禁用控件但保留原配置，
            # 用户下次重新开启字幕背景后，可以继续使用之前保存的圆角偏好。
            rounded_background_disabled = (
                subtitle_settings_disabled or not subtitle_background_enabled
            )
            st.session_state.setdefault(
                "rounded_subtitle_background_checkbox",
                saved_rounded_subtitle_background,
            )
            selected_rounded_subtitle_background = st.checkbox(
                tr("Rounded Subtitle Background"),
                help=tr("Rounded Subtitle Background Help"),
                disabled=rounded_background_disabled,
                key="rounded_subtitle_background_checkbox",
            )
            params.rounded_subtitle_background = (
                selected_rounded_subtitle_background
                if subtitle_background_enabled
                else False
            )
            if not subtitle_settings_disabled and subtitle_background_enabled:
                _set_runtime_config(
                    "ui",
                    "rounded_subtitle_background",
                    selected_rounded_subtitle_background,
                )

            if video.subtitle_colors_are_indistinguishable(params):
                # 同色配置仍然是合法的用户选择，因此只在字幕设置区域就近提示，
                # 不阻止生成。用户可以根据实际视觉需求决定是否继续。
                st.warning(tr("Subtitle Colors Are Indistinguishable"))

            subtitle_preview_text = params.video_script or params.video_subject
            selected_font_path = os.path.join(font_dir, params.font_name)
            if (
                params.subtitle_enabled
                and subtitle_preview_text
                and not video.subtitle_font_supports_text(
                    selected_font_path, subtitle_preview_text
                )
            ):
                st.warning(tr("Subtitle Font Does Not Support Text"))

            if st.button(
                tr("Restore Default Subtitle Settings"),
                key="restore_default_subtitle_settings",
                icon=":material/restart_alt:",
                on_click=reset_subtitle_settings,
                width="stretch",
            ):
                st.toast(tr("Default Subtitle Settings Restored"))


def _stage_task_audio(audio_path, audio_bytes):
    """Publish task-local audio only after the complete write succeeds."""
    descriptor = None
    staged_path = None
    try:
        descriptor, staged_path = tempfile.mkstemp(
            dir=os.path.dirname(audio_path), prefix=".task-audio-", suffix=".tmp"
        )
        with os.fdopen(descriptor, "wb") as file:
            descriptor = None  # The file context now owns the descriptor.
            file.write(audio_bytes)
        os.replace(staged_path, audio_path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if staged_path is not None:
            try:
                os.unlink(staged_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(f"failed to remove staged task audio: {exc}")


def _render_stock_policy_preflight(policy, video_terms, video_source="pexels"):
    policy = tm.animated_explainer.resolve_stock_media_policy(policy)
    provider_order = material.stock_video_provider_order(video_source)
    scope = " → ".join(provider.title() for provider in provider_order) or "no configured stock provider"
    if policy == "auto_eligible":
        st.caption(
            f"Effective stock policy: Automatic · provider order: {scope}. "
            "Search relevance is not visual verification."
        )
        return
    st.caption(f"Effective stock policy: Manual · provider order: {scope} · only hash-approved clips can render.")
    if not provider_order:
        st.info("Choose a configured stock-video provider before preparing review clips.")
        return
    terms = [term.strip() for term in re.split(r"[,，\n]", str(video_terms or "")) if term.strip()]
    if st.button("Prepare stock clips for review", key="prepare_stock_review"):
        if not terms:
            st.warning("Enter stock keywords before preparing footage for review.")
        else:
            review_id = st.session_state.get("stock_preflight_task_id") or str(uuid4())
            st.session_state["stock_preflight_task_id"] = review_id
            report = []
            unique_terms = list(dict.fromkeys(terms))
            max_queries = tm.animated_explainer.MAX_STORYBOARD_QUERIES
            if len(unique_terms) > max_queries:
                st.warning(
                    f"Only the first {max_queries} unique search terms can receive stock footage; "
                    f"{len(unique_terms) - max_queries} scene(s) will use illustrations."
                )
            for query in unique_terms[:max_queries]:
                for provider in provider_order:
                    candidates = tm.animated_explainer._cached_stock_candidates(query, provider)
                    if not candidates:
                        candidates = tm.animated_explainer._search_and_cache_stock_candidate(
                            query, provider, review_id, minimum_duration=3,
                            stock_media_policy="manual_review",
                        )
                    for candidate in candidates:
                        review = candidate.get("visual_review") or visual_review.sample_clip(
                            candidate["path"], review_id, str(candidate["asset_id"]),
                            usable_seconds=min(float(candidate["duration"]), 5.0),
                            video_aspect="16:9",
                        )
                        report.append({
                            "provider": provider, "asset_id": candidate["asset_id"],
                            "query": query, "sha256": candidate["sha256"],
                            "local_path": candidate["path"],
                            "source_page": candidate["source_page"],
                            "license": candidate["license"],
                            "approval_status": "human_review_required",
                            "visual_review": review,
                        })
            review_path = Path(utils.task_dir(review_id)) / "top-panel-storyboard.json"
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            st.session_state["stock_preflight_terms"] = terms
    review_id = st.session_state.get("stock_preflight_task_id")
    if review_id and st.session_state.get("stock_preflight_terms") == terms:
        _render_savage_stock_review(review_id)


def _render_generation_controls(
    params, uploaded_files, uploaded_audio_file, uploaded_bgm_file, voice_mode
):
    """
    校验生成依赖、提交任务，并渲染日志与成片结果。

    返回本次页面执行是否成功提交了新任务。提交前已经请求非阻塞保存，调用方
    据此跳过页面末尾的重复请求。主脚本必须及时结束，定时 Fragment 才能持续
    刷新进度和任务日志。
    """
    restore_upload_requirements = st.session_state.get(
        "task_restore_upload_requirements", {}
    )
    has_local_materials = bool(
        uploaded_files or st.session_state.get("local_video_materials", [])
    )
    has_custom_audio = bool(uploaded_audio_file)
    unmet_restore_requirements = _get_unmet_restore_upload_requirements(
        restore_upload_requirements,
        video_source=params.video_source,
        voice_name=params.voice_name or "",
        has_local_materials=has_local_materials,
        has_custom_audio=has_custom_audio,
        voice_mode=voice_mode,
    )
    if "local_materials" in unmet_restore_requirements:
        st.warning(tr("Task Restore Local Materials Warning"))
    if "custom_audio" in unmet_restore_requirements:
        st.warning(tr("Task Restore Custom Audio Warning"))
    if restore_upload_requirements and not unmet_restore_requirements:
        # 用户已重新上传文件，或主动切换了素材来源/音色。此时历史任务的上传依赖
        # 已经得到明确处理，清除标记，避免后续普通生成继续显示旧提示。
        st.session_state.pop("task_restore_upload_requirements", None)

    _render_settings_transfer(params)

    if params.effective_visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER:
        _render_stock_policy_preflight(
            params.stock_media_policy, params.video_terms, params.video_source,
        )

    start_button = st.button(
        tr("Generate Video"),
        width="stretch",
        type="primary",
        key="generate_video_button",
        on_click=_prepare_generation_task,
    )
    render_onboarding_tour()
    if start_button:
        _save_runtime_config()
        task_id = st.session_state.get("pending_generation_task_id") or str(uuid4())
        _add_active_generation_task(
            task_id,
            subject=params.video_subject or params.video_script or task_id,
        )
        if not params.video_subject and not params.video_script:
            _remove_active_generation_task(task_id)
            st.error(tr("Video Script and Subject Cannot Both Be Empty"))
            st.stop()

        voxcpm_reference_audio = None
        voxcpm_prompt_audio = None
        voxcpm_prompt_text = ""
        if voice.is_voxcpm_voice(params.voice_name or ""):
            reference_audio_error = st.session_state.get(
                VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY
            )
            if reference_audio_error:
                _remove_active_generation_task(task_id)
                st.error(
                    tr("VoxCPM Reference Audio Invalid").format(
                        error=reference_audio_error
                    )
                )
                st.stop()
            voxcpm_reference_audio = _get_voxcpm_reference_audio()
            prompt_validation_error = _get_voxcpm_prompt_validation_error()
            if prompt_validation_error:
                _remove_active_generation_task(task_id)
                st.error(
                    tr("VoxCPM Prompt Invalid").format(
                        error=prompt_validation_error
                    )
                )
                st.stop()
            voxcpm_prompt_audio = _get_voxcpm_effective_prompt_audio()
            voxcpm_prompt_text = _get_voxcpm_prompt_text()

        if params.video_source not in [
            "auto",
            "pexels",
            "pixabay",
            "coverr",
            "wavespeed",
            "volcengine_seedance",
            "ofox",
            "metaso_minimax",
            "muapi",
            "loomloom",
            "openai_image",
            "local",
        ]:
            _remove_active_generation_task(task_id)
            st.error(tr("Please Select a Valid Video Source"))
            st.stop()

        if params.video_source == "pexels" and not config.app.get(
            "pexels_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Pexels API Key"))
            st.stop()

        if params.video_source == "pixabay" and not config.app.get(
            "pixabay_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Pixabay API Key"))
            st.stop()

        if params.video_source == "coverr" and not config.app.get(
            "coverr_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Coverr API Key"))
            st.stop()

        if params.video_source == "wavespeed" and not config.app.get(
            "wavespeed_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the WaveSpeed API Key"))
            st.stop()

        if params.video_source == "wavespeed" and not st.session_state.get(
            "wavespeed_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm WaveSpeed Charge Required"))
            st.stop()

        if params.video_source == "volcengine_seedance" and not (
            volcengine_seedance.is_enabled(
                config.snapshot_config_with_pending(config.app)
            )
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Volcano Engine Ark API Key"))
            st.stop()

        if params.video_source == "volcengine_seedance" and not st.session_state.get(
            "volcengine_seedance_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm Volcano Engine Seedance Charge Required"))
            st.stop()

        if params.video_source == "ofox" and not (
            ofox.is_enabled(config.snapshot_config_with_pending(config.app))
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the OFox API Key"))
            st.stop()

        if params.video_source == "ofox" and not st.session_state.get(
            "ofox_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm OFox Charge Required"))
            st.stop()

        if params.video_source == "metaso_minimax" and not (
            metaso_minimax.is_enabled(
                config.snapshot_config_with_pending(config.app)
            )
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Metaso MiniMax API Key"))
            st.stop()

        if params.video_source == "metaso_minimax" and not st.session_state.get(
            "metaso_minimax_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm Metaso MiniMax Charge Required"))
            st.stop()

        if params.video_source == "muapi" and not (
            muapi.is_enabled(config.snapshot_config_with_pending(config.app))
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the MuAPI API Key"))
            st.stop()

        if params.video_source == "muapi" and not st.session_state.get(
            "muapi_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm MuAPI Charge Required"))
            st.stop()

        if params.video_source == "openai_image" and not material.is_openai_image_enabled(
            config.snapshot_config_with_pending(config.app)
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Configure the OpenAI Image Source"))
            st.stop()

        loomloom_video_request = None
        if params.video_source == "loomloom":
            current_batch, current_signature = _current_loomloom_video_quote_context(
                params
            )
            quoted_batch = st.session_state.get("loomloom_video_batch")
            quote_result = st.session_state.get("loomloom_video_quote")
            quote_is_current = bool(
                current_batch is not None
                and isinstance(quoted_batch, loomloom.LoomLoomVideoBatch)
                and quote_result is not None
                and st.session_state.get("loomloom_video_input_signature")
                == current_signature
            )
            if not quote_is_current:
                _remove_active_generation_task(task_id)
                st.error(tr("AI Video Quote Required"))
                st.stop()
            if not st.session_state.get("loomloom_video_confirm_charge", False):
                _remove_active_generation_task(task_id)
                st.error(tr("Confirm AI Video Charge Required"))
                st.stop()
            try:
                video_backend = _create_loomloom_video_backend()
                loomloom_video_request = loomloom.LoomLoomConfirmedVideoRequest(
                    settings=video_backend.settings,
                    batch=current_batch,
                    listing_version_id=quote_result.listing_version_id,
                    client_request_id=st.session_state[
                        "loomloom_video_client_request_id"
                    ],
                )
                loomloom_video_request.validate()
            except (loomloom.LoomLoomError, ValueError) as exc:
                _remove_active_generation_task(task_id)
                st.error(str(exc))
                st.stop()

        if (
            params.bgm_type == "sonilo"
            and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
            and not sonilo_service.is_enabled()
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Sonilo API Key Required"))
            st.stop()

        if (
            params.bgm_type == "elevenlabs"
            and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
            and not elevenlabs_music_service.is_enabled()
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("ElevenLabs API Key Required"))
            st.stop()

        if params.video_source == "local" and not has_local_materials:
            # 本地素材为空时继续执行会先产生 TTS/字幕，最后才在素材预处理阶段失败。
            # 在任务启动前拦截，可以避免无意义的 API 调用和中间文件。
            _remove_active_generation_task(task_id)
            st.error(tr("Please Upload Local Materials First"))
            st.stop()

        if voice_mode == VOICE_MODE_UPLOAD and not uploaded_audio_file:
            # 上传音频是用户显式选择的配音方式，缺少文件时不能静默退回 TTS。
            # 在任务启动前拦截，避免产生与用户选择不一致的成片。
            _remove_active_generation_task(task_id)
            st.error(tr("Please Upload Voiceover File First"))
            st.stop()

        if "custom_audio" in unmet_restore_requirements:
            # 历史自定义音频不能自动回填。用户尚未重新上传且也没有主动更换音色时，
            # 必须阻止静默退回 TTS，否则重新生成的结果会与原任务语音不一致。
            _remove_active_generation_task(task_id)
            st.error(tr("Task Restore Custom Audio Warning"))
            st.stop()

        if uploaded_bgm_file and bgm_service.should_use_bgm(
            params.bgm_type, params.bgm_volume
        ):
            try:
                saved_bgm_name = bgm_service.save_bgm_upload(
                    uploaded_bgm_file.name, uploaded_bgm_file
                )
            except bgm_service.BgmUploadError as exc:
                _remove_active_generation_task(task_id)
                logger.warning(f"WebUI background music upload rejected: {str(exc)}")
                st.error(tr("Invalid Background Music"))
                st.stop()
            except bgm_service.BgmServiceError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"WebUI background music upload failed: {str(exc)}")
                st.error(tr("Background Music Validation Failed"))
                st.stop()
            # 保存成功后只把文件名写入任务参数。视频服务会在两个 BGM 白名单
            # 目录中重新解析，避免把服务器绝对路径持久化或展示给用户。
            params.bgm_file = saved_bgm_name
        elif uploaded_bgm_file:
            # 0 音量时视频服务不会使用任何 BGM，因此不再把已经预览的上传文件
            # 持久化到 storage。用户之后调高音量时可直接再次点击生成完成保存。
            params.bgm_file = ""

        if uploaded_audio_file:
            try:
                task_dir = utils.task_dir(task_id)
                custom_audio_path = _build_uploaded_file_path(
                    uploaded_audio_file,
                    task_dir,
                    CUSTOM_AUDIO_EXTENSIONS,
                    "custom-audio",
                )
                # Voiceover uploads previously bypassed the same full-decode
                # and size checks used for background-music uploads. Validate
                # before storing or scheduling the generation task.
                bgm_service.validate_bgm_upload(
                    uploaded_audio_file.name, uploaded_audio_file
                )
                _stage_task_audio(custom_audio_path, uploaded_audio_file.getbuffer())
            except OSError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"failed to persist uploaded task audio: {exc}")
                st.error(tr("Video Generation Failed"))
                st.stop()
            except bgm_service.BgmUploadError as exc:
                _remove_active_generation_task(task_id)
                logger.warning(f"WebUI custom audio upload rejected: {exc}")
                st.error(str(exc))
                st.stop()
            except bgm_service.BgmServiceError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"WebUI custom audio validation failed: {exc}")
                st.error(str(exc))
                st.stop()
            except ValueError:
                _remove_active_generation_task(task_id)
                st.error(tr("Unsupported Upload File Type"))
                st.stop()
            params.custom_audio_file = custom_audio_path

        if uploaded_files:
            # 每次重新上传时都以本次选择的素材为准，避免旧素材不断重复追加。
            try:
                params.video_materials, persisted_local_materials = (
                    _save_uploaded_local_materials(uploaded_files)
                )
            except material_upload_service.MaterialUploadError as exc:
                _remove_active_generation_task(task_id)
                logger.warning(f"WebUI local material upload rejected: {exc}")
                st.error(str(exc))
                st.stop()
            except material_upload_service.MaterialServiceError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"WebUI local material upload failed: {exc}")
                st.error(str(exc))
                st.stop()
            # 将已上传并保存到本地的视频素材写入会话，供后续只改文案时直接复用。
            st.session_state["local_video_materials"] = persisted_local_materials
        elif (
            params.video_source == "local" and st.session_state["local_video_materials"]
        ):
            # 当用户没有重新上传文件时，复用最近一次已经保存到磁盘的本地素材列表。
            params.video_materials = []
            for material_entry in st.session_state["local_video_materials"]:
                m = MaterialInfo()
                m.provider = material_entry.get("provider", "local")
                m.url = material_entry.get("url", "")
                m.duration = material_entry.get("duration", 0)
                if m.url:
                    params.video_materials.append(m)

        # All visual, narration, TTS, and caption controls have been applied by
        # this point; now freeze the immutable settings snapshot for the job.
        generation_settings_service.resolve_task_settings(params)
        reusable_voice_preview = _get_reusable_full_voice_preview(
            params,
            voice_mode,
        )
        if reusable_voice_preview:
            # 试听缓存只存在当前 Streamlit 会话。提交前把音频写入目标任务目录，
            # 后台线程随后只读取任务自己的文件；即使页面 rerun、浏览器关闭或
            # 用户试听其它音色，也不会影响已经入队的生成任务。
            try:
                preview_audio_file = os.path.join(
                    utils.task_dir(task_id),
                    "audio.mp3",
                )
                _stage_task_audio(preview_audio_file, reusable_voice_preview["audio_bytes"])
            except OSError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"failed to persist preview task audio: {exc}")
                st.error(tr("Video Generation Failed"))
                st.stop()
            reusable_voice_preview.pop("audio_bytes")
            reusable_voice_preview["audio_file"] = preview_audio_file
            logger.info(
                f"reuse full voice preview for task: "
                f"task_id={task_id}, duration={reusable_voice_preview['duration']:.2f}s"
            )

        try:
            st.toast(tr("Generating Video"))
            logger.info(tr("Start Generating Video"))
            logger.info(utils.to_json(params))
            webui_task.submit_generation(
                task_id=task_id,
                params=params,
                capture_logs=not config.ui.get("hide_log", False),
                voice_preview=reusable_voice_preview,
                loomloom_video_request=loomloom_video_request,
                voxcpm_reference_audio=voxcpm_reference_audio,
                voxcpm_prompt_audio=voxcpm_prompt_audio,
                voxcpm_prompt_text=voxcpm_prompt_text,
            )
            if loomloom_video_request is not None:
                # 一个报价只允许提交一次。后台请求自带稳定幂等 ID；提交成功后
                # 清除页面报价，下一次生成必须重新询价和确认。
                st.session_state["loomloom_video_batch"] = None
                st.session_state["loomloom_video_quote"] = None
                st.session_state["loomloom_video_input_signature"] = ""
                st.session_state["loomloom_video_client_request_id"] = ""
        except Exception:
            _remove_active_generation_task(task_id)
            st.error(tr("Video Generation Failed"))
            st.stop()

        st.session_state["current_generation_task_id"] = task_id
        logger.info(f"WebUI generation task submitted: task_id={task_id}")

    _render_current_generation_task()
    return start_button


def _render_application():
    """按固定顺序渲染顶部栏、弹窗、生成表单和任务结果。"""
    if st.session_state.pop("_apply_style_defaults_before_render", False):
        _on_visual_style_changed()
    _render_top_bar()

    workspace = st.radio(
        "Workspace", ["Video Generator", "Channel Autopilot"], horizontal=True,
        index=1, key="mpt_workspace",
    )
    if workspace == "Channel Autopilot":
        _render_channel_autopilot()
        return

    if st.session_state.get("settings_dialog_open", False):
        _render_settings_dialog()

    if _apply_pending_settings_preset():
        st.success(tr("Settings Preset Imported"))

    restore_applied = _apply_pending_task_restore()
    restore_candidate_id = st.session_state.get("task_restore_candidate_id")
    if restore_candidate_id:
        _render_task_restore_dialog(restore_candidate_id)
    restore_succeeded = st.session_state.pop("task_restore_succeeded", False)
    if restore_applied or restore_succeeded:
        st.success(tr("Task Configuration Loaded"))

    with st.container(key="main_settings_grid"):
        panel = st.columns(4)
    left_panel = panel[0]
    middle_panel = panel[1]
    audio_panel = panel[2]
    right_panel = panel[3]

    params = VideoParams(video_subject="")
    params.visual_style = VISUAL_STYLE_VALUES.get(
        st.session_state.get(
            "visual_style_select",
            VISUAL_STYLE_LABELS["savage_animated_explainer"],
        ),
        "auto",
    )
    params.stock_video_style = (
        "satisfying_asmr_cutting" if params.visual_style == "asmr_cutting" else "default"
    )
    params.asmr_categories = st.session_state.get(
        "asmr_categories_select", ["kinetic_sand", "soap"]
    )
    params.match_materials_to_script = bool(
        st.session_state.get("match_materials_to_script", False)
    )
    _render_script_settings(left_panel, params)

    uploaded_files = _render_video_settings(middle_panel, params)
    uploaded_audio_file, uploaded_bgm_file, voice_mode = _render_audio_settings(
        audio_panel, params
    )

    _render_subtitle_settings(right_panel, params)

    generation_submitted = _render_generation_controls(
        params,
        uploaded_files,
        uploaded_audio_file,
        uploaded_bgm_file,
        voice_mode,
    )

    # 生成分支在启动后台线程前已经请求过保存。普通控件交互继续请求非阻塞保存；
    # 如果后台任务正在使用配置，配置层会在任务结束时自动应用并落盘最新值。
    if not generation_submitted:
        _save_runtime_config()


def _autopilot_connection_label(profile, service):
    if profile.publishing_backend == "youtube_direct":
        connection = service.youtube_connections.connection_for_channel(profile.channel_id)
        if connection and connection.get("state") == "connected":
            return connection.get("channel_title") or profile.display_name
        return "YouTube connection needed"
    return profile.upload_post_username or "Upload-Post account needed"


def _autopilot_save_profile(profile, **changes):
    from app.models.channel_profile import ChannelProfile
    updated = ChannelProfile.model_validate({**profile.model_dump(mode="json"), **changes})
    channel_profiles.verify_publisher_binding(updated)
    return channel_profiles.store.update(profile.channel_id, updated, profile.version)


def _manual_video_topic(topic, script):
    """Keep manual narration usable without requiring a separate topic field."""
    topic = str(topic or "").strip()
    if topic:
        return topic
    narration = " ".join(str(script or "").split())
    if not narration:
        return ""
    first_sentence = re.split(r"(?<=[।.!?])\s+", narration, maxsplit=1)[0]
    return first_sentence[:200].strip() or narration[:200].strip()


def _autopilot_create_video(profile, topic, script, duration, aspect, style, language,
                            review_ai_draft=False, narrator_preset=None,
                            profanity_level=None, fact_verification_policy=None,
                            savage_quality_policy=None, background_music_enabled=None,
                            source_settings=None, parent_job_id=None, video_terms=None,
                            intro_enabled=None, outro_enabled=None,
                            stock_media_policy=None):
    from app.models.autopilot import ContentPlanCreate

    service = autopilot_service.get_service()
    target = int(duration)
    topic = _manual_video_topic(topic, script)
    manual_overrides = {
        "target_duration_seconds": target,
        "video_aspect": aspect,
        "language": language,
        "font_name": "NotoSansDevanagari-VF.ttf" if language == "hi" else profile.font_name,
    }
    if intro_enabled is not None:
        manual_overrides["intro_enabled"] = bool(intro_enabled)
    if outro_enabled is not None:
        manual_overrides["outro_enabled"] = bool(outro_enabled)
    if isinstance(source_settings, dict) and isinstance(source_settings.get("channel_branding"), dict):
        manual_overrides["channel_branding"] = source_settings["channel_branding"]
    inherited_keys = {
        "voice_name", "tts_model", "subtitle_enabled", "subtitle_position",
        "subtitle_display_mode", "subtitle_animation", "voice_rate", "voice_volume",
        "allow_unverified_publication", "duration_enforcement", "hindi_alignment_policy",
        "bgm_type", "bgm_file", "bgm_volume", "stock_video_style", "video_source",
        "asmr_categories", "tts_provider", "stock_media_policy",
    }
    manual_overrides.update({
        key: value for key, value in (source_settings or {}).items()
        if key in inherited_keys and value is not None
    })
    if stock_media_policy is not None:
        manual_overrides["stock_media_policy"] = stock_media_policy
    # An explicitly edited duration uses the form's target and its normal range.
    if source_settings and int(source_settings.get("target_duration_seconds", target)) != target:
        manual_overrides.pop("duration_enforcement", None)
    if narrator_preset is not None:
        manual_overrides["narrator_preset"] = narrator_preset
    if profanity_level is not None:
        manual_overrides["profanity_level"] = profanity_level
    if fact_verification_policy is not None:
        manual_overrides["fact_verification_policy"] = fact_verification_policy
    if savage_quality_policy is not None:
        manual_overrides["savage_quality_policy"] = savage_quality_policy
    if background_music_enabled is False:
        manual_overrides.update({"bgm_type": "", "bgm_file": "", "bgm_volume": 0.0})
    try:
        plan = service.plan(profile.channel_id, ContentPlanCreate(
            topic=topic.strip(), script=script.strip(),
            min_duration_seconds=(
                source_settings.get("min_duration_seconds", max(1, target - 5))
                if source_settings and int(source_settings.get("target_duration_seconds", target)) == target
                else max(1, target - 5)
            ),
            max_duration_seconds=(
                source_settings.get("max_duration_seconds", target)
                if source_settings and int(source_settings.get("target_duration_seconds", target)) == target
                else target
            ),
            visual_style_override=style,
        ), manual_overrides=manual_overrides, source_job_id=parent_job_id)
    except autopilot_service.DuplicateContent as exc:
        if parent_job_id:
            raise autopilot_service.AutopilotError(
                "This new version matches another item in channel history. Edit the topic or narration and try again."
            ) from exc
        raise
    # Keep an already-created draft visible if a later step fails.
    st.session_state["autopilot_last_plan"] = plan.plan_id
    if (review_ai_draft or (plan.duration_enforcement == "strict" and not
            plan.min_duration_seconds <= plan.duration_estimate_seconds <= plan.max_duration_seconds)):
        st.session_state["autopilot_pending_plan"] = plan.plan_id
        st.session_state["autopilot_pending_video_terms"] = video_terms
        return None
    service.ready(profile.channel_id, plan.plan_id, False)
    job = service.create_job(
        profile.channel_id, plan.plan_id, parent_job_id=parent_job_id,
        video_terms=video_terms,
    )
    st.session_state["autopilot_last_job"] = job.job_id
    # Persist the job handle before submitting it. The worker can fail quickly
    # (for example, on a TTS 429); if start_job then raises while returning its
    # refreshed row, the UI must still render that job's recorded failure stage.
    st.session_state["autopilot_active_job"] = job.job_id
    service.start_job(job.job_id)
    st.session_state.pop("autopilot_pending_plan", None)
    return job


def _autopilot_resume_draft(profile, topic, script, duration, aspect, style, language,
                            review_ai_draft=False, narrator_preset=None,
                            profanity_level=None, fact_verification_policy=None,
                            savage_quality_policy=None, background_music_enabled=None,
                            video_terms=None, intro_enabled=None, outro_enabled=None,
                            stock_media_policy=None):
    """Continue an exact, unstarted channel draft without making a duplicate plan."""
    service = autopilot_service.get_service()
    normalized = autopilot_service.normalize_content(topic)
    existing = next((plan for plan in service.list_manual_drafts(profile.channel_id)
                     if autopilot_service.normalize_content(plan.topic) == normalized), None)
    if existing is None:
        return None
    overrides = {
        "target_duration_seconds": int(duration), "video_aspect": aspect,
        "visual_style": style, "language": language,
        "font_name": "NotoSansDevanagari-VF.ttf" if language == "hi" else profile.font_name,
    }
    if stock_media_policy is not None:
        overrides["stock_media_policy"] = stock_media_policy
    if intro_enabled is not None:
        overrides["intro_enabled"] = bool(intro_enabled)
    if outro_enabled is not None:
        overrides["outro_enabled"] = bool(outro_enabled)
    if narrator_preset is not None:
        overrides["narrator_preset"] = narrator_preset
    if profanity_level is not None:
        overrides["profanity_level"] = profanity_level
    if fact_verification_policy is not None:
        overrides["fact_verification_policy"] = fact_verification_policy
    if savage_quality_policy is not None:
        overrides["savage_quality_policy"] = savage_quality_policy
    if background_music_enabled is False:
        overrides.update({"bgm_type": "", "bgm_file": "", "bgm_volume": 0.0})
    existing = service.prepare_manual_draft(
        profile.channel_id, existing.plan_id, overrides, script=script
    )
    if (review_ai_draft or (existing.duration_enforcement == "strict" and not
            existing.min_duration_seconds <= existing.duration_estimate_seconds <= existing.max_duration_seconds)):
        st.session_state["autopilot_pending_plan"] = existing.plan_id
        return False
    if existing.state == "planned":
        service.ready(profile.channel_id, existing.plan_id, True)
    job = service.create_job(
        profile.channel_id, existing.plan_id, video_terms=video_terms
    )
    st.session_state["autopilot_last_job"] = job.job_id
    service.start_job(job.job_id)
    st.session_state.pop("autopilot_pending_plan", None)
    return job


def _autopilot_upcoming_schedule(profile):
    if not profile.publications_per_week:
        return []
    zone = ZoneInfo(profile.timezone)
    local_now = datetime.now(zone)
    choices = sorted((day, hour, minute) for day in profile.schedule_weekdays
                     for hour, minute in (tuple(map(int, value.split(":")))
                                          for value in profile.schedule_times_local))
    selected = {choices[min(len(choices) - 1, int(i * len(choices) / profile.publications_per_week))]
                for i in range(profile.publications_per_week)}
    upcoming = []
    for offset in range(14):
        date = local_now.date() + timedelta(days=offset)
        for day, hour, minute in sorted(selected):
            if date.weekday() != day:
                continue
            slot = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone)
            if slot > local_now and slot.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None) == slot.replace(tzinfo=None):
                upcoming.append(slot)
    return upcoming[:5]


@st.cache_data(show_spinner=False)
def _autopilot_thumbnail(video_path, modified_at):
    try:
        command = [utils.get_ffmpeg_binary(), "-hide_banner", "-loglevel", "error"]
        for seek in ("1", "0"):
            try:
                result = subprocess.run(
                    [*command, "-ss", seek, "-i", video_path, "-frames:v", "1",
                     "-vf", "scale=480:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                    capture_output=True, timeout=10, check=False,
                )
            except subprocess.TimeoutExpired:
                continue
            if result.returncode == 0 and result.stdout:
                return result.stdout
        return None
    except (OSError, ValueError, TypeError):
        return None


def _autopilot_failure_help(message):
    lower = message.lower()
    if "post-generation qa failed" in lower and "audio_track" in lower:
        return (
            "The video was rendered, but its audio track did not pass the final check. "
            "The file is still available to preview or download; retry to render a new attempt."
        )
    if "script" in lower and ("provider" in lower or "draft" in lower):
        return f"{message} Check the text provider in Settings, or enter your own script."
    if "audio" in lower or "voice" in lower:
        return f"{message} Check the selected voice and its provider in Settings."
    if "material" in lower or "footage" in lower:
        if "insufficient distinct asmr footage" in lower:
            return f"{message} There is not enough unique matching ASMR stock for this narration. Use general stock footage from My Videos, or supply more licensed ASMR clips."
        return f"{message} Try another stock source or a more specific topic."
    if "duration" in lower:
        return f"{message} Adjust the script length and create a new version."
    return f"{message} Open Advanced / Diagnostics for the full job details."


@st.fragment(run_every="2s")
def _render_autopilot_active_job(job_id, channel_id):
    service = autopilot_service.get_service()
    try:
        job = service.store.get_job(job_id)
    except LookupError:
        st.warning("The submitted job is no longer available. Check My Videos or Diagnostics.")
        return
    if job.channel_id != channel_id:
        return
    task_state = sm.state.get_task(job.mpt_task_id) if job.mpt_task_id else None
    if job.state == "failed":
        st.error(_autopilot_failure_help(job.failure_details or "Video generation failed."))
        if task_state and task_state.get("failed_stage") == "audio":
            profile = channel_profiles.store.get(channel_id)
            st.warning(
                f"Narration failed at the audio step. The saved voice is “{profile.voice_name or 'default'}”. "
                "Open Channels and select a voice supported by the configured TTS provider, then retry generation."
            )
        return
    if job.state == "generated":
        st.success("Video is ready. Open My Videos to preview and approve it.")
        return
    progress = int((task_state or {}).get("progress") or 0)
    step = ("Rendering video" if progress >= 50 else "Finding visuals" if progress >= 40
            else "Preparing subtitles" if progress >= 30 else "Generating narration" if progress >= 20
            else "Finding search terms" if progress >= 10 else "Preparing script")
    st.info(f"Video job is {job.state}. {step} · {progress}%")
    st.progress(progress, text=step)


@st.fragment(run_every="3s")
def _render_autopilot_videos_fragment(channel_id):
    from webui.autopilot_view import group_video_attempts

    service = autopilot_service.get_service()
    jobs = service.store.list_jobs(channel_id, 500)
    all_groups = group_video_attempts(jobs)
    available_gallery_ids = {attempts[0].job_id for attempts in all_groups}
    selected_key = f"gallery_selected_jobs_{channel_id}"
    st.session_state[selected_key] = [
        job_id for job_id in st.session_state.get(selected_key, [])
        if job_id in available_gallery_ids
    ]
    drafts = service.list_manual_drafts(channel_id)
    if not jobs and not drafts:
        st.info("No videos yet. Create your first video in Create Video.")
        return
    receipts = {}
    for item in service.store.list_publications(channel_id, 1000):
        receipts.setdefault(item["job_id"], {}).setdefault(
            item.get("platform", "youtube"), []
        ).append(item)
    selection_mode_key = f"gallery_selection_mode_{channel_id}"
    selected_key = f"gallery_selected_jobs_{channel_id}"
    selection_mode = bool(st.session_state.get(selection_mode_key, False))
    header_cols = st.columns([1.5, 2.5, 1.2, 0.9], vertical_alignment="center")
    header_cols[0].markdown(f"### My Videos · {len(all_groups)}")
    query = header_cols[1].text_input(
        "Search videos", key=f"gallery_search_{channel_id}",
        placeholder="Search videos", label_visibility="collapsed",
    ).strip().casefold()
    sort_order = header_cols[2].selectbox(
        "Sort videos", ["Newest first", "Oldest first"],
        key=f"gallery_sort_{channel_id}", label_visibility="collapsed",
    )
    if header_cols[3].button(
        "Done" if selection_mode else "Select",
        key=f"{selection_mode_key}_toggle",
        help="Finish selecting videos" if selection_mode else "Select videos for bulk actions",
        width="stretch",
    ):
        st.session_state[selection_mode_key] = not selection_mode
        if selection_mode:
            st.session_state[selected_key] = []
        st.rerun(scope="app")
    if query:
        jobs = [job for job in jobs if query in (job.topic or "").casefold()]
    groups = group_video_attempts(jobs)
    groups.sort(
        key=lambda attempts: attempts[0].created_at,
        reverse=sort_order == "Newest first",
    )
    if not groups and not drafts:
        st.info("No matching videos. Try another title or create your first video.")
    page_key = f"gallery_page_{channel_id}"
    visible_groups, page, page_count = _autopilot_gallery_page(
        groups, st.session_state.get(page_key, 1)
    )
    st.session_state[page_key] = page
    if page_count > 1:
        prev_col, page_col, next_col = st.columns([1, 3, 1])
        if prev_col.button("Previous", key=f"{page_key}_prev", disabled=page <= 1):
            st.session_state[page_key] = page - 1
            st.rerun(scope="app")
        page_col.caption(f"Page {page} of {page_count} · {len(groups)} videos")
        if next_col.button("Next", key=f"{page_key}_next", disabled=page >= page_count):
            st.session_state[page_key] = page + 1
            st.rerun(scope="app")
    selected_ids = set(st.session_state.get(selected_key, []))
    if selection_mode:
        action_cols = st.columns([1, 1.25, 2, 5])
        if action_cols[0].button(
            f"Select all {len(groups)} filtered videos",
            key=f"{selection_mode_key}_all",
            help="Select every video matching the current search, across all gallery pages.",
        ):
            st.session_state[selected_key] = [attempts[0].job_id for attempts in groups]
            st.rerun(scope="app")
        if action_cols[1].button("Deselect all", key=f"{selection_mode_key}_clear"):
            st.session_state[selected_key] = []
            st.rerun(scope="app")
        action_cols[2].markdown(f"**{len(selected_ids)} selected**")
        if selected_ids:
            result_cols = st.columns([1, 3, 5])
            if result_cols[0].button("Delete Permanently", key=f"{selection_mode_key}_delete"):
                st.session_state["gallery_bulk_delete_confirmed"] = False
                st.session_state[f"gallery_bulk_delete_{channel_id}"] = sorted(selected_ids)
                st.rerun(scope="app")
            selected_jobs = [attempts[0] for attempts in all_groups
                             if attempts[0].job_id in selected_ids]
            selected_outputs = [(job, _autopilot_video_output(job)) for job in selected_jobs]
            selected_outputs = [(job, path) for job, path in selected_outputs if path]
            if selected_outputs:
                try:
                    source_bytes = sum(
                        os.path.getsize(path) for _job, path in selected_outputs
                        if os.path.isfile(path)
                    )
                    if source_bytes > 256 * 1024 * 1024:
                        result_cols[1].caption("Selected videos exceed the 256 MB ZIP download limit.")
                    else:
                        result_cols[1].download_button(
                            "Download selected (ZIP)",
                            lambda outputs=tuple(selected_outputs): _build_autopilot_gallery_zip(outputs),
                            file_name="my-videos.zip", mime="application/zip",
                            key=f"{selection_mode_key}_download",
                        )
                except ValueError as exc:
                    result_cols[1].caption(str(exc))
            result_cols[2].caption(
                f"{len(selected_outputs)} completed video(s) available to download."
            )
    playable = [
        (attempts[0], output)
        for attempts in visible_groups
        if (output := _autopilot_video_output(attempts[0]))
    ]
    with st.container(key="my_video_gallery"):
        for start in range(0, len(visible_groups), 6):
            with st.container(key=f"my_video_gallery_row_{start}"):
                columns = st.columns(6, gap="small", vertical_alignment="top")
                for column, attempts in zip(columns, visible_groups[start:start + 6]):
                    latest = attempts[0]
                    with column:
                        _render_autopilot_video_attempt(
                            latest, channel_id, receipts.get(latest.job_id), gallery=True,
                            playable_jobs=playable,
                            selection_mode=st.session_state.get(selection_mode_key, False),
                        )
                        if len(attempts) > 1:
                            with st.expander(f"Earlier attempts · {len(attempts) - 1}", expanded=False):
                                for attempt in attempts[1:]:
                                    attempt_receipts = receipts.get(attempt.job_id, {})
                                    attempt_receipt = next(iter(
                                        attempt_receipts.get("youtube", [])
                                    ), None)
                                    _render_autopilot_video_attempt(
                                        attempt, channel_id, attempt_receipt
                                    )
    if st.session_state.get(selection_mode_key):
        st.session_state[selected_key] = sorted(selected_ids)
    if drafts:
        with st.expander(f"Saved drafts · {len(drafts)}", expanded=not jobs):
            for draft in drafts:
                with st.container(border=True):
                    st.subheader(draft.topic)
                    st.caption("Saved draft · Not generating yet")
                    if st.button("Continue this draft", key=f"simple_resume_{draft.plan_id}"):
                        st.session_state["autopilot_edit_job"] = (channel_id, draft.topic, draft.script)
                        st.rerun(scope="app")
                    st.caption("Loads the topic and script into Create Video for review.")


def _autopilot_gallery_page(groups, page, page_size=18):
    page_size = max(1, int(page_size))
    page_count = max(1, math.ceil(len(groups) / page_size))
    page = min(max(int(page), 1), page_count)
    start = (page - 1) * page_size
    return groups[start:start + page_size], page, page_count


def _render_autopilot_videos(channel_id):
    """Keep the live-updating grid isolated so playback dialogs stay mounted."""
    from webui.autopilot_view import group_video_attempts

    _render_autopilot_videos_fragment(channel_id)
    bulk_result = st.session_state.get("gallery_bulk_delete_result")
    if bulk_result:
        removed, failures = bulk_result
        if removed:
            st.success(
                f"Permanently deleted {len(removed)} local video record(s) and available files. "
                "YouTube and Instagram posts were not changed."
            )
        else:
            st.info("No gallery records were removed. Review the per-video results below.")
        for topic, failure in failures:
            st.warning(f"{topic}: {failure}")
        if st.button("Dismiss bulk result", key="gallery_bulk_result_dismiss"):
            st.session_state.pop("gallery_bulk_delete_result", None)
            st.rerun(scope="app")
    service = autopilot_service.get_service()
    jobs = service.store.list_jobs(channel_id, 500)
    query = str(st.session_state.get(f"gallery_search_{channel_id}", "")).strip().casefold()
    if query:
        jobs = [job for job in jobs if query in (job.topic or "").casefold()]
    groups = group_video_attempts(jobs)
    groups.sort(
        key=lambda attempts: attempts[0].created_at,
        reverse=st.session_state.get(f"gallery_sort_{channel_id}", "Newest first") == "Newest first",
    )
    playable = [
        (attempts[0], output)
        for attempts in groups
        if (output := _autopilot_video_output(attempts[0]))
    ]
    player_id = st.session_state.get("gallery_play_job_id")
    if player_id:
        selected_player = next(
            ((job, path) for job, path in playable if job.job_id == player_id), None
        )
        if selected_player:
            _render_autopilot_gallery_video_modal(playable)
        else:
            st.session_state.pop("gallery_play_job_id", None)
    details_id = st.session_state.get("gallery_details_job_id")
    if details_id:
        selected_details = next((job for job in jobs if job.job_id == details_id), None)
        if selected_details:
            _render_autopilot_gallery_details_dialog(selected_details)
        else:
            st.session_state.pop("gallery_details_job_id", None)
    delete_id = st.session_state.get("gallery_delete_job_id")
    if delete_id:
        selected_delete = next(
            (job for job in service.store.list_jobs(channel_id, 500)
             if job.job_id == delete_id), None
        )
        if selected_delete:
            _render_autopilot_gallery_delete_dialog(selected_delete, channel_id)
        else:
            st.session_state.pop("gallery_delete_job_id", None)
    download_id = st.session_state.get("gallery_download_job_id")
    if download_id:
        selected_download = next(
            ((job, path) for job, path in playable if job.job_id == download_id), None
        )
        if selected_download:
            _render_autopilot_gallery_download_dialog(*selected_download)
        else:
            st.session_state.pop("gallery_download_job_id", None)
    bulk_delete_key = f"gallery_bulk_delete_{channel_id}"
    bulk_delete_ids = st.session_state.get(bulk_delete_key)
    if bulk_delete_ids:
        bulk_groups = [attempts for attempts in group_video_attempts(
            service.store.list_jobs(channel_id, 500)
        ) if attempts[0].job_id in set(bulk_delete_ids)]
        if bulk_groups:
            _render_autopilot_gallery_bulk_delete_dialog(bulk_groups, channel_id)
        else:
            st.session_state.pop(bulk_delete_key, None)
    publish_confirmation = st.session_state.get("gallery_publish_confirm")
    if publish_confirmation:
        try:
            publish_job = service.store.get_job(publish_confirmation[0])
        except LookupError:
            st.session_state.pop("gallery_publish_confirm", None)
        else:
            if publish_job.channel_id == channel_id:
                _render_autopilot_gallery_publish_dialog(publish_job, *publish_confirmation[1:])


def _render_autopilot_video_attempt(job, channel_id, receipt, gallery=False,
                                    playable_jobs=None, selection_mode=False):
    if gallery:
        _render_autopilot_gallery_card(
            job, channel_id, receipt, playable_jobs or [], selection_mode=selection_mode,
        )
        return

    service = autopilot_service.get_service()
    task_state = sm.state.get_task(job.mpt_task_id) if job.mpt_task_id else None
    progress = int((task_state or {}).get("progress") or 0)
    step = ("Rendering video" if progress >= 50 else "Finding visuals" if progress >= 40
            else "Preparing subtitles" if progress >= 30 else "Generating narration" if progress >= 20
            else "Finding search terms" if progress >= 10 else "Preparing script")
    status = ("Published" if receipt and receipt["state"] == "published" else
              "Failed" if job.state == "failed" else
              "Ready" if job.state == "generated" else
              "Rendering" if job.state == "generating" and progress >= 50 else
              "Generating" if job.state in {"queued", "generating"} else "Planning")
    with st.container(border=True):
        st.subheader(job.topic)
        requested_style = job.params_snapshot.get("visual_style", job.visual_style)
        effective_style = (task_state or {}).get("visual_style_selection", {}).get(
            "effective_mode", job.effective_visual_style
        )
        style_reason = (task_state or {}).get("visual_style_selection", {}).get(
            "reason", job.visual_style_reason
        )
        style_label = {
            "auto": "AUTO", "asmr_cutting": "ASMR Cutting",
            "savage_animated_explainer": "Savage Animated Explainer",
            "relevant_stock": "Relevant Stock",
        }.get(requested_style, requested_style)
        task_label = f" · Task {job.mpt_task_id[:8]}" if job.mpt_task_id else ""
        st.caption(
            f"{status} · {job.created_at.strftime('%d %b %Y, %H:%M')} · "
            f"{job.profile_snapshot.get('display_name', '')} · {style_label} · "
            f"Job {job.job_id[:8]}{task_label}"
        )
        st.caption(
            f"Requested: {style_label} · selected: {effective_style or 'pending'} · "
            f"{style_reason or 'selection reason pending preflight'}"
        )
        fact_policy = job.params_snapshot.get(
            "fact_verification_policy", job.profile_snapshot.get("fact_verification_policy", "advisory")
        )
        raw_fact_state = (task_state or {}).get(
            "fact_verification_state", getattr(job, "fact_verification_state", "unverified")
        )
        fact_status = {
            "source_verified": "verified", "unsupported": "unverified",
            "contradicted": "contradicted", "unavailable": "unavailable",
            "verification_error": "unavailable", "unverified": "unverified",
        }.get(raw_fact_state, "unverified")
        st.caption(f"Fact check: {fact_status} · mode: {fact_policy}")
        if (job.profile_snapshot.get("publishing_policy") in {"automatic_public", "scheduled_public"}
                and fact_status != "verified"):
            if job.profile_snapshot.get("allow_unverified_publication", False):
                st.warning("Public publishing is opted in for unverified facts; the upload title will be labeled [UNVERIFIED FACTS].")
            else:
                st.warning("Unverified facts will prevent unattended public publishing. Video generation can still complete.")
        if task_state and task_state.get("materials"):
            with st.expander("Selected footage"):
                records = []
                try:
                    manifest = json.loads(
                        (Path(utils.task_dir(job.mpt_task_id)) / "script.json").read_text(encoding="utf-8")
                    ).get("material_sources", [])
                    for item in manifest:
                        if not isinstance(item, dict):
                            continue
                        records.append({
                            key: item[key]
                            for key in (
                                "local_file", "provider", "search_term", "duration",
                                "source_page", "license", "license_name",
                            )
                            if item.get(key) not in (None, "")
                        })
                except (OSError, ValueError, TypeError):
                    records = []
                if records:
                    st.write(records)
                else:
                    st.write([Path(str(path)).name for path in task_state["materials"]])
        if job.state in {"queued", "generating"}:
            st.progress(progress if job.state == "generating" else 0,
                        text=f"{progress}% · {step}")
        if job.failure_details:
            st.error(_autopilot_failure_help(job.failure_details))
            if (job.state == "failed" and not job.output_artifacts and not receipt
                    and job.profile_snapshot.get("publishing_policy") == "manual"
                    and "Insufficient distinct ASMR footage" in job.failure_details):
                if st.button("Use general stock footage for this video", key=f"simple_stock_fallback_{job.job_id}",
                             help="Creates a new manual video attempt with the same script and destination. Channel defaults stay as saved."):
                    try:
                        replacement = service.retry_with_stock_footage(job.job_id)
                        st.session_state["autopilot_active_job"] = replacement.job_id
                        st.success("New video attempt queued with general stock footage and your original script.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Stock footage attempt could not start: {exc}")
            if task_state and task_state.get("failed_stage") == "audio":
                saved_voice = job.profile_snapshot.get("voice_name") or "default"
                st.warning(
                    f"Audio failed with saved voice “{saved_voice}”. This voice was sent to Azure/Edge TTS and rejected. "
                    "The Gemini key and voice saved in Video Generator settings do not change this job's saved settings. "
                    "Save the Gemini voice in Channels, then create a new attempt below."
                )
        if job.state == "failed" and "Insufficient distinct ASMR footage" not in (job.failure_details or ""):
            if st.button("Retry video", type="primary", key=f"simple_retry_{job.job_id}",
                         help="Retry this video with its saved script and settings."):
                try:
                    service.retry_job(job.job_id)
                    st.session_state["autopilot_active_job"] = job.job_id
                    st.rerun()
                except Exception as exc:
                    st.error(f"Retry could not start: {exc}")
        if receipt and receipt.get("failure_details"):
            st.error(f"YouTube upload: {receipt['failure_details']}")
        for artifact in job.output_artifacts:
            if os.path.isfile(artifact):
                thumbnail = _autopilot_thumbnail(artifact, os.path.getmtime(artifact))
                preview_col, action_col = st.columns([1, 5], vertical_alignment="top")
                if thumbnail:
                    preview_col.image(thumbnail, width=112)
                with action_col:
                    with open(artifact, "rb") as video_file:
                        st.download_button("Download video", video_file, file_name=Path(artifact).name,
                                           mime="video/mp4", key=f"download_{job.job_id}_{artifact}")
                    with st.expander("Preview rendered video", expanded=False):
                        st.video(artifact, width=420)
                break
        with st.expander("Script and more actions", expanded=False):
            st.text_area("Narration script", job.script, disabled=True, key=f"simple_script_{job.job_id}")
            if st.button("Edit as a new version", key=f"simple_edit_{job.job_id}"):
                st.session_state["autopilot_edit_job"] = (channel_id, job.topic, job.script)
                st.rerun()
            if (job.state == "failed" and task_state and task_state.get("failed_stage") == "audio"
                    and st.button("Create new attempt with saved channel settings",
                                  key=f"simple_retry_current_{job.job_id}",
                                  help="Creates a separate job with current channel settings; the failed job remains unchanged.")):
                try:
                    replacement = service.retry_with_current_channel_settings(job.job_id)
                    st.session_state["autopilot_active_job"] = replacement.job_id
                    st.success("New video attempt queued with current channel settings.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"New attempt could not start: {exc}")
            if job.state in {"failed", "generated", "cancelled"}:
                if receipt:
                    st.caption("This job has an upload receipt, so it is retained for YouTube upload recovery and audit history.")
                elif job.intended_publication_slot:
                    st.caption("Scheduled jobs are retained in channel history.")
                else:
                    st.caption("Permanently deletes this video's local job history and generated files. Remote social posts remain online.")
                    confirm_delete = st.checkbox(
                        "I understand local publication tracking will also be deleted",
                        key=f"simple_delete_confirm_{job.job_id}",
                    )
                    if st.button(
                        "Delete permanently",
                        key=f"simple_delete_{job.job_id}",
                        disabled=not confirm_delete,
                    ):
                        try:
                            result = service.store.delete_job(job.job_id, channel_id)
                            st.success("Video permanently deleted from MPT. Remote social posts remain online.")
                            for detail in result.get("cleanup_errors", []):
                                st.warning(f"Some local files could not be removed: {detail}")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Job could not be deleted: {exc}")
        if job.state == "ready" and st.button("Start generation", key=f"simple_start_{job.job_id}"):
            try:
                service.start_job(job.job_id)
                st.rerun()
            except Exception as exc:
                st.error(f"Generation could not start: {exc}")
        if job.state == "generated":
            with service.store._connection() as conn:
                qa = conn.execute("SELECT state FROM job_qa WHERE job_id=?", (job.job_id,)).fetchone()
            manual_policy = job.profile_snapshot.get("publishing_policy") == "manual"
            manual_publish_ready = bool(
                manual_policy and qa and qa["state"] in {"qa_passed", "approved"}
            )
            if qa and qa["state"] == "qa_passed" and not manual_policy:
                if st.button("Approve video", key=f"simple_approve_{job.job_id}"):
                    try:
                        service.review_job(job.job_id, "approve")
                        st.rerun()
                    except Exception as exc:
                        st.toast(str(exc), icon=":material/error:")
            if qa and (qa["state"] == "approved" or manual_publish_ready) and manual_policy:
                receipt_state = (receipt or {}).get("state")
                if receipt_state in {"uploading", "unknown"}:
                    st.info("YouTube upload is being reconciled. Duplicate submissions are blocked.")
                elif receipt_state == "scheduled":
                    st.info("Scheduled on YouTube")
                elif receipt_state == "published":
                    st.success("Uploaded to YouTube")
                elif receipt_state == "failed":
                    st.error("YouTube upload failed. Retry is available after the previous attempt is confirmed safe.")
                elif _gallery_youtube_connected(job, service):
                    if st.button("Upload privately to YouTube", key=f"simple_publish_{job.job_id}"):
                        try:
                            service.publish_job(job.job_id)
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Private upload failed: {exc}")
            elif not qa or qa["state"] not in {"qa_passed", "approved"}:
                st.warning("Rendering completed, but quality checks need attention. See Advanced / Diagnostics.")
        if receipt and receipt.get("provider_url"):
            st.link_button("Open on YouTube", receipt["provider_url"])


def _autopilot_video_output(job):
    storage_root = os.path.realpath(utils.storage_dir())
    artifacts = list(job.output_artifacts or [])
    artifacts.sort(key=lambda path: not str(path).rsplit("/", 1)[-1].startswith("final-"))
    for item in artifacts:
        try:
            candidate = os.path.realpath(os.fspath(item))
            if os.path.commonpath((storage_root, candidate)) != storage_root:
                continue
            if os.path.isfile(candidate):
                return candidate
        except (OSError, TypeError, ValueError):
            continue
    return None


def _dismiss_autopilot_gallery_player():
    st.session_state.pop("gallery_play_job_id", None)


def _select_autopilot_gallery_player(job_id):
    st.session_state["gallery_play_job_id"] = job_id


def _queue_autopilot_video_edit(job_id, channel_id):
    """Persist the edit source before Streamlit reruns and select Create Video."""
    st.session_state["autopilot_main_tabs"] = "Create Video"
    st.session_state["simple_autopilot_channel"] = channel_id
    st.session_state[f"gallery_actions_{job_id}"] = False
    try:
        job = autopilot_service.get_service().store.get_job(job_id)
        if job.channel_id != channel_id:
            raise LookupError("source video does not belong to the selected channel")
        st.session_state["autopilot_edit_job"] = {
            "job_id": job.job_id,
            "channel_id": job.channel_id,
            "topic": job.topic,
            "script": job.script,
            "params_snapshot": dict(job.params_snapshot or {}),
        }
        st.session_state.pop("autopilot_edit_error", None)
    except LookupError as exc:
        st.session_state.pop("autopilot_edit_job", None)
        st.session_state["autopilot_edit_error"] = str(exc)
    except Exception as exc:
        logger.exception("failed to prepare video version from source task {}", job_id)
        st.session_state.pop("autopilot_edit_job", None)
        st.session_state["autopilot_edit_error"] = (
            f"Video {job_id[:8]} could not be loaded for editing ({type(exc).__name__})."
        )


def _apply_autopilot_edit_form_state(state, edited, profile):
    """Copy source values once; later Streamlit reruns keep the user's edits."""
    if not isinstance(edited, dict) or edited.get("channel_id") != profile.channel_id:
        return
    source_id = edited.get("job_id")
    if state.get("autopilot_edit_loaded_source_id") == source_id:
        return
    settings = edited.get("params_snapshot") or {}
    channel_id = profile.channel_id
    state[f"simple_topic_{channel_id}"] = edited.get("topic", "")
    state[f"simple_draft_{channel_id}"] = edited.get("script", "")
    state[f"simple_script_mode_{channel_id}"] = "Write or review my script"
    state[f"simple_duration_{channel_id}"] = int(
        settings.get("target_duration_seconds") or profile.target_duration_seconds
    )
    state[f"simple_format_{channel_id}"] = (
        "Shorts (9:16)" if settings.get("video_aspect", "9:16") == "9:16"
        else "Landscape (16:9)"
    )
    style = settings.get("visual_style")
    style_labels = {value: label for label, value in VISUAL_STYLE_VALUES.items()}
    if style in style_labels:
        state[f"simple_style_{channel_id}"] = style_labels[style]
        state[f"simple_style_user_selected_{channel_id}"] = True
        state[f"simple_active_style_{channel_id}"] = style
        state[f"simple_style_defaults_version_{channel_id}"] = 1
    state[f"simple_language_{channel_id}"] = (
        "Hindi" if str(settings.get("video_language") or settings.get("language") or "hi").lower().startswith("hi")
        else "English"
    )
    state[f"simple_narrator_{channel_id}"] = {
        "standard": "Standard", "desi_savage_aggressive": "Desi Savage Hindi",
    }.get(settings.get("narrator_preset"), "Desi Savage Hindi")
    state[f"simple_profanity_{channel_id}"] = {
        "mild": "Mild", "savage_monetization_aware": "Savage — Monetization Aware",
        "savage_unfiltered": "Savage Unfiltered",
    }.get(settings.get("profanity_level"), "Savage — Monetization Aware")
    state[f"simple_fact_policy_{channel_id}"] = settings.get(
        "fact_verification_policy", "off"
    )
    state[f"simple_stock_policy_{channel_id}"] = settings.get(
        "stock_media_policy", getattr(profile, "stock_media_policy", "auto_eligible")
    )
    state[f"simple_background_music_{channel_id}"] = bool(
        settings.get("bgm_type") or settings.get("bgm_file")
    )
    state["autopilot_edit_loaded_source_id"] = source_id


def _close_autopilot_gallery_action_menu(job_id):
    st.session_state[f"gallery_actions_{job_id}"] = False


@st.dialog(
    "Watch video", width="small", dismissible=True,
    on_dismiss=_dismiss_autopilot_gallery_player,
)
def _render_autopilot_gallery_video_modal(playable_jobs):
    player_id = st.session_state.get("gallery_play_job_id")
    current = next(
        (i for i, (item, _) in enumerate(playable_jobs) if item.job_id == player_id), None
    )
    if current is None:
        st.warning("This video is no longer available.")
        return

    job, video_path = playable_jobs[current]
    aspect_value = str(job.params_snapshot.get("video_aspect", "9:16"))
    aspect_class = (
        "landscape" if aspect_value in {"16:9", "4:3"}
        else "square" if aspect_value == "1:1" else "portrait"
    )
    st.caption(job.topic)
    with st.container(key=f"gallery_video_modal_player_{aspect_class}"):
        st.video(video_path, width="stretch")
    previous_col, position_col, next_col = st.columns([1, 2, 1], vertical_alignment="center")
    previous_col.button(
        "Previous", key="gallery_player_previous", icon=":material/arrow_back:",
        type="tertiary", disabled=current <= 0, help="Play previous video",
        width="stretch", on_click=_select_autopilot_gallery_player,
        args=(playable_jobs[current - 1][0].job_id,) if current > 0 else (),
    )
    position_col.caption(f"{current + 1} of {len(playable_jobs)}")
    next_col.button(
        "Next", key="gallery_player_next", icon=":material/arrow_forward:",
        type="tertiary", disabled=current >= len(playable_jobs) - 1,
        help="Play next video", width="stretch",
        on_click=_select_autopilot_gallery_player,
        args=(playable_jobs[current + 1][0].job_id,)
        if current < len(playable_jobs) - 1 else (),
    )


@st.dialog("Delete video permanently?", width="small", dismissible=False)
def _render_autopilot_gallery_delete_dialog(job, channel_id):
    st.write(
        f"Permanently delete **{job.topic}** from MPT, including its local job history "
        "and generated files? Any YouTube or Instagram post stays online."
    )
    st.caption("Local publication tracking is removed. An upload still being reconciled must finish first.")
    cancel_col, confirm_col = st.columns(2)
    if cancel_col.button("Cancel", key="gallery_delete_cancel"):
        st.session_state.pop("gallery_delete_job_id", None)
        st.rerun(scope="app")
    if confirm_col.button("Delete permanently", key="gallery_delete_confirm", type="primary"):
        try:
            result = autopilot_service.get_service().store.delete_job(job.job_id, channel_id)
            st.session_state.pop("gallery_delete_job_id", None)
            if result.get("cleanup_errors"):
                st.warning("The local job was deleted, but some files could not be removed: "
                           + "; ".join(result["cleanup_errors"]))
            else:
                st.toast("Video permanently deleted from MPT. Remote posts remain online.")
            st.rerun(scope="app")
        except Exception as exc:
            st.error(f"Video could not be removed: {exc}")


def _gallery_youtube_connected(job, service):
    profile = job.profile_snapshot or {}
    connection_id = profile.get("youtube_connection_id")
    destination_id = profile.get("youtube_channel_id")
    if (profile.get("publishing_backend", "youtube_direct") != "youtube_direct"
            or not connection_id or not destination_id):
        return False
    try:
        connection = service.youtube_connections.get_connection(connection_id)
    except Exception:
        return False
    return bool(connection.get("local_channel_id") == job.channel_id
                and connection.get("youtube_channel_id") == destination_id)


def _gallery_upload_post_destination(job, service, platform, refresh=False):
    profile_username = (job.profile_snapshot.get("upload_post_username")
                        or config.app.get("upload_post_username", ""))
    api_key = config.app.get("upload_post_api_key", "")
    if not profile_username or not api_key or not config.app.get("upload_post_enabled", False):
        return None
    cache_key = hashlib.sha256(f"{api_key}:{profile_username}:{platform}".encode()).hexdigest()
    cache = st.session_state.setdefault("gallery_upload_post_status_cache", {})
    cached = cache.get(cache_key)
    if (not refresh and isinstance(cached, tuple) and time.time() - cached[0] < 60):
        return cached[1]
    result = service.upload_post_destination(job, platform)
    cache[cache_key] = (time.time(), result)
    return result


def _gallery_instagram_destination(job, service, refresh=False):
    return _gallery_upload_post_destination(job, service, "instagram", refresh)


def _gallery_youtube_destination(job, service):
    if job.profile_snapshot.get("publishing_backend") == "youtube_direct":
        if not _gallery_youtube_connected(job, service):
            return None
        return {"username": job.profile_snapshot.get("youtube_channel_id"),
                "handle": job.profile_snapshot.get("youtube_channel_id")}
    return _gallery_upload_post_destination(job, service, "youtube")


def _toggle_autopilot_gallery_selection(channel_id, job_id):
    key = f"gallery_selected_jobs_{channel_id}"
    selected = set(st.session_state.get(key, []))
    if job_id in selected:
        selected.remove(job_id)
    else:
        selected.add(job_id)
    st.session_state[key] = sorted(selected)


@st.dialog("Delete selected videos permanently?", width="small", dismissible=False)
def _render_autopilot_gallery_bulk_delete_dialog(groups, channel_id):
    attempt_count = sum(len(attempts) for attempts in groups)
    st.write(
        f"Permanently delete {len(groups)} selected video(s) and their {attempt_count} "
        "generation record(s), local publication tracking, and generated files? "
        "Remote YouTube and Instagram posts stay online."
    )
    st.caption("Uploads with uncertain outcomes must be reconciled before their local tracking can be deleted.")
    confirmed = st.checkbox("I understand this permanently deletes local video data", key="gallery_bulk_delete_confirmed")
    cancel_col, delete_col = st.columns(2)
    if cancel_col.button("Cancel", key="gallery_bulk_delete_cancel"):
        st.session_state.pop(f"gallery_bulk_delete_{channel_id}", None)
        st.rerun(scope="app")
    if delete_col.button("Delete permanently", key="gallery_bulk_delete_submit", type="primary", disabled=not confirmed):
        service = autopilot_service.get_service()
        removed, failures = [], []
        for attempts in groups:
            for job in attempts:
                try:
                    result = service.store.delete_job(job.job_id, channel_id)
                    removed.append(job.job_id)
                    if result.get("cleanup_errors"):
                        failures.append((job.topic, "Some generated files could not be removed: "
                                         + "; ".join(result["cleanup_errors"])))
                except Exception as exc:
                    failures.append((job.topic, str(exc)))
        selected_key = f"gallery_selected_jobs_{channel_id}"
        st.session_state[selected_key] = []
        st.session_state[f"gallery_selection_mode_{channel_id}"] = False
        st.session_state.pop(f"gallery_bulk_delete_{channel_id}", None)
        st.session_state["gallery_bulk_delete_result"] = (removed, failures)
        st.rerun(scope="app")


def _build_autopilot_gallery_zip(job_paths, max_total_bytes=256 * 1024 * 1024):
    """Build a bounded archive from completed gallery artifacts."""
    total = 0
    for _job, path in job_paths:
        if not os.path.isfile(path):
            continue
        total += os.path.getsize(path)
        if total > max_total_bytes:
            raise ValueError("Selected videos exceed the 256 MB ZIP download limit.")
    buffer = BytesIO()
    used_names = set()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for job, path in job_paths:
            if not os.path.isfile(path):
                continue
            filename = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(path).name).strip("._") or "video.mp4"
            arcname = f"{job.job_id[:8]}-{filename}"
            if arcname in used_names:
                arcname = f"{job.job_id}-{filename}"
            used_names.add(arcname)
            archive.write(path, arcname)
    return buffer.getvalue()


@st.dialog("Confirm upload", width="medium", dismissible=False)
def _render_autopilot_gallery_publish_dialog(job, platform, destination_account_id):
    service = autopilot_service.get_service()
    destination = (_gallery_instagram_destination(job, service)
                   if platform == "instagram" else _gallery_youtube_destination(job, service))
    if not destination or destination.get("username") != destination_account_id:
        st.error("This destination is no longer connected. Refresh its status in Channel settings before uploading.")
        if st.button("Close", key="gallery_publish_destination_close"):
            st.session_state.pop("gallery_publish_confirm", None)
            st.rerun(scope="app")
        return
    handle = destination.get("handle") or destination.get("display_name") or destination_account_id
    caption_key = None
    if platform == "youtube":
        st.write(f"YouTube destination: **{handle}**")
        st.info("Manual YouTube publication uses Private visibility. This action does not enable Autopilot or public posting.")
        metadata = service.get_youtube_metadata(job.job_id)
        if metadata is None:
            st.info("No current saved YouTube caption is available. Generate one to continue.")
            generate_col, close_col = st.columns(2)
            if generate_col.button("Generate YouTube caption", key="gallery_publish_caption_generate"):
                try:
                    with st.spinner("Generating the YouTube caption…"):
                        service.ensure_youtube_metadata(job.job_id)
                    st.rerun(scope="app")
                except Exception as exc:
                    st.error(f"Caption could not be prepared: {exc}")
            if close_col.button("Close", key="gallery_publish_caption_close"):
                st.session_state.pop("gallery_publish_confirm", None)
                st.rerun(scope="app")
            return
        st.write(f"YouTube title: **{metadata.get('title', '')}**")
        caption_key = (
            f"gallery_youtube_caption_{job.job_id}_"
            f"{metadata.get('narration_fingerprint', '')[:8]}_"
            f"{metadata.get('context_fingerprint', '')[:8]}"
        )
        if caption_key not in st.session_state:
            st.session_state[caption_key] = metadata["caption"]
        st.markdown("**AI-generated YouTube caption**")
        st.text_area(
            "Caption · up to 50 words including five hashtags",
            key=caption_key, height=120,
            help="Generated from the narration and this video's saved channel settings. Edit before posting if needed.",
        )
        caption_words = len(str(st.session_state[caption_key]).split())
        caption_hashtags = re.findall(
            r"(?<!\w)#[\w]+", str(st.session_state[caption_key]), flags=re.UNICODE
        )
        st.caption(f"{caption_words}/50 words · {len(caption_hashtags)}/5 hashtags")
        caption_validation_errors = service.youtube_caption_validation_errors(
            job.job_id, str(st.session_state[caption_key]),
        )
        if caption_validation_errors:
            st.caption("Caption needs: " + "; ".join(caption_validation_errors))
    else:
        st.write(f"**{job.topic}**")
        st.write(f"Instagram destination: **@{handle.lstrip('@')}**")
        st.info("This publishes a Reel now. Visibility follows the connected Instagram account's audience settings; MPT will not change them.")
    cancel_col, upload_col = st.columns(2)
    if cancel_col.button("Cancel", key="gallery_publish_cancel"):
        st.session_state.pop("gallery_publish_confirm", None)
        st.rerun(scope="app")
    caption_invalid = bool(
        platform == "youtube"
        and caption_validation_errors
    )
    if upload_col.button(
        "Post to YouTube" if platform == "youtube" else "Upload now",
        key="gallery_publish_confirm_submit", type="primary", disabled=caption_invalid,
    ):
        try:
            if platform == "youtube":
                service.save_youtube_caption(job.job_id, st.session_state[caption_key])
            result = service.publish_job(job.job_id, platform=platform,
                                         destination_account_id=destination_account_id)
            st.session_state["gallery_publish_result"] = result
        except Exception as exc:
            st.session_state["gallery_publish_result"] = {
                "job_id": job.job_id, "platform": platform, "state": "failed",
                "error": str(exc),
            }
        st.session_state.pop("gallery_publish_confirm", None)
        st.rerun(scope="app")


@st.dialog("Video details", width="medium", dismissible=False)
def _render_autopilot_gallery_details_dialog(job):
    task_state = sm.state.get_task(job.mpt_task_id) if job.mpt_task_id else None
    fact_policy = job.params_snapshot.get(
        "fact_verification_policy", job.profile_snapshot.get("fact_verification_policy", "off")
    )
    fact_state = (task_state or {}).get(
        "fact_verification_state", getattr(job, "fact_verification_state", "unverified")
    )
    st.caption(
        f"Created {job.created_at.strftime('%d %b %Y, %H:%M')} · "
        f"Job {job.job_id[:8]}"
        + (f" · Task {job.mpt_task_id[:8]}" if job.mpt_task_id else "")
        + (f" · Version of {job.parent_job_id[:8]}" if getattr(job, "parent_job_id", None) else "")
    )
    st.caption(f"Fact check · {fact_policy} · {fact_state}")
    if job.failure_details:
        st.error(_autopilot_failure_help(job.failure_details))
    st.text_area("Narration script", job.script, disabled=True,
                 key=f"gallery_script_{job.job_id}", height=180)
    if job.mpt_task_id:
        _render_savage_stock_review(job.mpt_task_id)
    if st.button("Close details", key="gallery_details_close"):
        st.session_state.pop("gallery_details_job_id", None)
        st.rerun(scope="app")


@st.dialog("Download video", width="small", dismissible=False)
def _render_autopilot_gallery_download_dialog(job, video_path):
    st.caption(job.topic)
    with open(video_path, "rb") as video_file:
        st.download_button(
            "Download video", video_file, file_name=Path(video_path).name,
            mime="video/mp4", key="gallery_download_ready", on_click="ignore",
            width="stretch", icon=":material/download:",
        )
    if st.button("Close", key="gallery_download_close"):
        st.session_state.pop("gallery_download_job_id", None)
        st.rerun(scope="app")


def _render_autopilot_gallery_card(job, channel_id, receipt, playable_jobs,
                                   selection_mode=False):
    service = autopilot_service.get_service()
    task_state = sm.state.get_task(job.mpt_task_id) if job.mpt_task_id else None
    output = _autopilot_video_output(job)
    publication_rows = receipt if isinstance(receipt, dict) else {}
    youtube_receipts = publication_rows.get("youtube", [])
    instagram_receipts = publication_rows.get("instagram", [])
    if isinstance(youtube_receipts, dict):
        youtube_receipts = [youtube_receipts]
    if isinstance(instagram_receipts, dict):
        instagram_receipts = [instagram_receipts]
    youtube_destination = _gallery_youtube_destination(job, service)
    youtube_receipt = next((row for row in youtube_receipts
                            if youtube_destination and row.get("destination_account_id") == youtube_destination.get("username")), None)
    instagram_destination = _gallery_instagram_destination(job, service)
    instagram_receipt = next((row for row in instagram_receipts
                              if instagram_destination and row.get("destination_account_id") == instagram_destination.get("username")), None)
    status = ("Failed" if job.state == "failed" else
              "Ready" if job.state == "generated" else
              "Rendering" if job.state in {"queued", "generating"} else
              "Draft")
    if getattr(job, "parent_job_id", None):
        status = f"Version · {status}"
    duration = job.actual_duration_seconds or job.duration_estimate_seconds
    duration_label = f"{int(duration // 60)}:{int(duration % 60):02d}" if duration else "Duration pending"

    with st.container(key=f"gallery_card_{job.job_id}", border=True):
        status_col, menu_col = st.columns([5, 1], vertical_alignment="center")
        status_col.caption(status)
        for platform_label, publication in (("YouTube", youtube_receipt),
                                             ("Instagram", instagram_receipt)):
            if publication:
                platform_status = {"published": "Uploaded", "scheduled": "Scheduled",
                                   "uploading": "Uploading", "unknown": "Checking", "failed": "Failed"}.get(
                                       publication.get("state"), publication.get("state", "Status")
                                   )
                status_col.caption(f"{platform_label} · {platform_status}")
        with menu_col:
            with st.popover(
                "", icon=":material/more_vert:", type="tertiary", help="Video actions",
                key=f"gallery_actions_{job.job_id}", width=190, on_change="rerun",
            ):
                if output and not selection_mode and st.button("Play", key=f"gallery_menu_play_{job.job_id}",
                                         width="stretch", type="tertiary",
                                         icon=":material/play_arrow:",
                                         on_click=_close_autopilot_gallery_action_menu,
                                         args=(job.job_id,)):
                    st.session_state["gallery_play_job_id"] = job.job_id
                    st.rerun(scope="app")
                if output:
                    if st.button("Download", key=f"gallery_download_{job.job_id}",
                                 width="stretch", type="tertiary",
                                 icon=":material/download:",
                                 on_click=_close_autopilot_gallery_action_menu,
                                 args=(job.job_id,)):
                        st.session_state["gallery_download_job_id"] = job.job_id
                        st.rerun(scope="app")
                if st.button("View details", key=f"gallery_details_{job.job_id}",
                             width="stretch", type="tertiary", icon=":material/info:",
                             on_click=_close_autopilot_gallery_action_menu,
                             args=(job.job_id,)):
                    st.session_state["gallery_details_job_id"] = job.job_id
                    st.rerun(scope="app")
                if job.state == "failed":
                    if ("Insufficient distinct ASMR footage" in (job.failure_details or "")
                            and job.profile_snapshot.get("publishing_policy") == "manual"
                            and not job.output_artifacts):
                        if st.button("Use general stock footage", key=f"gallery_stock_fallback_{job.job_id}",
                                     width="stretch", type="tertiary",
                                     icon=":material/refresh:"):
                            try:
                                replacement = service.retry_with_stock_footage(job.job_id)
                                st.session_state["autopilot_active_job"] = replacement.job_id
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Stock footage attempt could not start: {exc}")
                    else:
                        if st.button("Retry video", key=f"gallery_retry_{job.job_id}",
                                     width="stretch", type="tertiary",
                                     icon=":material/refresh:"):
                            try:
                                service.retry_job(job.job_id)
                                st.session_state["autopilot_active_job"] = job.job_id
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Retry could not start: {exc}")
                    if task_state and task_state.get("failed_stage") == "audio":
                        if st.button("Retry with current channel settings",
                                     key=f"gallery_retry_settings_{job.job_id}",
                                     width="stretch", type="tertiary",
                                     icon=":material/settings_backup_restore:"):
                            try:
                                replacement = service.retry_with_current_channel_settings(job.job_id)
                                st.session_state["autopilot_active_job"] = replacement.job_id
                                st.rerun()
                            except Exception as exc:
                                st.error(f"New attempt could not start: {exc}")
                if job.state == "ready" and st.button(
                    "Start generation", key=f"gallery_start_{job.job_id}", width="stretch",
                    type="tertiary", icon=":material/play_arrow:",
                ):
                    try:
                        service.start_job(job.job_id)
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Generation could not start: {exc}")
                if st.button("Edit as a new version", key=f"gallery_edit_{job.job_id}",
                             width="stretch", type="tertiary", icon=":material/edit:",
                             on_click=_queue_autopilot_video_edit,
                             args=(job.job_id, channel_id)):
                    st.rerun(scope="app")
                if job.state == "generated":
                    with service.store._connection() as conn:
                        qa = conn.execute("SELECT state FROM job_qa WHERE job_id=?", (job.job_id,)).fetchone()
                    manual_policy = job.profile_snapshot.get("publishing_policy") == "manual"
                    manual_publish_ready = bool(
                        manual_policy and qa and qa["state"] in {"qa_passed", "approved"}
                    )
                    if qa and qa["state"] == "qa_passed" and not manual_policy:
                        if st.button("Approve video", key=f"gallery_approve_{job.job_id}",
                                     width="stretch", type="tertiary",
                                     icon=":material/check_circle:"):
                            try:
                                service.review_job(job.job_id, "approve")
                                st.rerun()
                            except Exception as exc:
                                st.toast(str(exc), icon=":material/error:")
                    if qa and (qa["state"] == "approved" or manual_publish_ready):
                        if (youtube_destination and job.profile_snapshot.get("publishing_policy") == "manual"
                                and (not youtube_receipt or youtube_receipt.get("state") == "failed")):
                            if st.button("Retry YouTube upload" if youtube_receipt else "Upload to YouTube",
                                         key=f"gallery_publish_youtube_{job.job_id}", width="stretch",
                                         type="tertiary", icon=":material/upload:",
                                         help="Manual YouTube uploads use Private visibility."):
                                st.session_state["gallery_publish_confirm"] = (
                                    job.job_id, "youtube", youtube_destination["username"]
                                )
                                st.rerun(scope="app")
                        if (instagram_destination
                                and (not instagram_receipt or instagram_receipt.get("state") == "failed")):
                            if st.button("Retry Instagram upload" if instagram_receipt else "Upload to Instagram",
                                         key=f"gallery_publish_instagram_{job.job_id}", width="stretch",
                                         type="tertiary", icon=":material/upload:",
                                         help="Publishes a Reel using this connected Instagram account's current audience settings."):
                                st.session_state["gallery_publish_confirm"] = (
                                    job.job_id, "instagram", instagram_destination["username"]
                                )
                                st.rerun(scope="app")
                if youtube_receipt and youtube_receipt.get("provider_url"):
                    st.link_button(
                        "View on YouTube", youtube_receipt["provider_url"], width="stretch",
                        type="tertiary", icon=":material/open_in_new:",
                    )
                if instagram_receipt and instagram_receipt.get("provider_url"):
                    st.link_button(
                        "View on Instagram", instagram_receipt["provider_url"], width="stretch",
                        type="tertiary", icon=":material/open_in_new:",
                    )
                if job.state in {"failed", "generated", "cancelled"}:
                    if receipt or job.intended_publication_slot:
                        st.caption("This record is retained for publication history.")
                    else:
                        if st.button("Remove from My Videos", key=f"gallery_delete_{job.job_id}",
                                     width="stretch", type="tertiary",
                                     icon=":material/delete:"):
                            st.session_state["gallery_delete_job_id"] = job.job_id
                            st.rerun(scope="app")

        with st.container(key=f"gallery_thumbnail_slot_portrait_{job.job_id}"):
            if output:
                thumbnail = _autopilot_thumbnail(output, os.path.getmtime(output))
                if thumbnail:
                    st.image(thumbnail, width="stretch")
                else:
                    st.markdown('<div class="mpt-gallery-placeholder">▣</div>', unsafe_allow_html=True)
            else:
                st.markdown('<div class="mpt-gallery-placeholder">▣</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="mpt-gallery-duration">{html.escape(duration_label)}</div>',
                unsafe_allow_html=True,
            )
            if selection_mode:
                selected = job.job_id in set(
                    st.session_state.get(f"gallery_selected_jobs_{channel_id}", [])
                )
                st.button(
                    "Deselect video" if selected else "Select video",
                    key=f"gallery_select_{job.job_id}",
                    on_click=_toggle_autopilot_gallery_selection,
                    args=(channel_id, job.job_id),
                )
                st.markdown(
                    '<div class="mpt-gallery-select-indicator is-selected">✓</div>'
                    if selected else '<div class="mpt-gallery-select-indicator"> </div>',
                    unsafe_allow_html=True,
                )
            else:
                if st.button("Play video", key=f"gallery_play_{job.job_id}"):
                    if output:
                        st.session_state["gallery_play_job_id"] = job.job_id
                        st.rerun(scope="app")
                    elif job.state == "failed":
                        st.toast(
                            "This failed attempt has no playable video. Use the menu to retry or view details.",
                            icon=":material/info:",
                        )
                    else:
                        st.toast("This video is not ready to play yet.", icon=":material/info:")

        with st.container(key=f"gallery_title_{job.job_id}"):
            st.text(job.topic)
        with st.container(key=f"gallery_progress_{job.job_id}"):
            if job.state in {"queued", "generating"}:
                progress = int((task_state or {}).get("progress") or 0)
                st.progress(progress, text=f"{progress}%")
            else:
                st.caption(" ")


def _render_channel_autopilot():
    st.title("Channel Autopilot")
    profiles = channel_profiles.store.list()
    service = autopilot_service.get_service()
    if not profiles:
        st.info("Create a channel profile, then connect its YouTube identity in Channels.")
        with st.form("simple_first_channel"):
            name = st.text_input("Channel name")
            language = st.selectbox("Narration language", ["Hindi", "English"])
            if st.form_submit_button("Create channel profile", type="primary"):
                try:
                    from app.models.channel_profile import ChannelProfile
                    created = channel_profiles.store.create(ChannelProfile(
                        display_name=name.strip(), language="hi" if language == "Hindi" else "en",
                        publishing_backend="youtube_direct", publishing_policy="manual",
                    ))
                    st.session_state["autopilot_select_after_create"] = created.channel_id
                    st.rerun()
                except Exception as exc:
                    st.error(f"Channel could not be created: {exc}")
        return
    profiles_by_id = {item.channel_id: item for item in profiles}
    new_selection = st.session_state.pop("autopilot_select_after_create", None)
    if new_selection in profiles_by_id:
        st.session_state["simple_autopilot_channel"] = new_selection
    edited = st.session_state.get("autopilot_edit_job")
    if isinstance(edited, dict) and edited.get("channel_id") in profiles_by_id:
        edit_channel = edited["channel_id"]
        st.session_state["simple_autopilot_channel"] = edit_channel
        _apply_autopilot_edit_form_state(
            st.session_state, edited, profiles_by_id[edit_channel]
        )
    edit_error = st.session_state.pop("autopilot_edit_error", None)
    if edit_error:
        st.error(edit_error)
    channel_id = st.selectbox("YouTube channel", list(profiles_by_id),
                              format_func=lambda key: f"{profiles_by_id[key].display_name} · {_autopilot_connection_label(profiles_by_id[key], service)}",
                              key="simple_autopilot_channel")
    profile = profiles_by_id[channel_id]
    destination = _autopilot_connection_label(profile, service)
    if destination in {"YouTube connection needed", "Upload-Post account needed"}:
        st.warning(f"Destination: {destination}. Connect this channel in Channels before uploading.")
    else:
        st.success(f"YouTube destination: {destination}")
    styles = VISUAL_STYLE_VALUES
    create_tab, videos_tab, schedule_tab, channels_tab, advanced_tab = st.tabs(
        ["Create Video", "My Videos", "Schedule", "Channels", "Advanced / Diagnostics"],
        key="autopilot_main_tabs", on_change="rerun",
    )

    if create_tab.open:
        st.subheader("Create one video")
        if isinstance(edited, dict) and edited.get("channel_id") == channel_id:
            st.info(
                f"Preparing a new version of “{edited.get('topic') or 'Untitled video'}” "
                f"(source {edited.get('job_id', '')[:8]}). Review the copied settings and narration; "
                "the original video will remain unchanged."
            )
        st.caption("Your saved channel settings are loaded. Generate an idea to fill both fields, or paste a topic and click Generate narration script.")
        topic_key = f"simple_topic_{channel_id}"
        draft_key = f"simple_draft_{channel_id}"
        generated_script_key = f"simple_ai_draft_{channel_id}"
        stock_terms_key = f"simple_stock_terms_{channel_id}"
        stock_policy_key = f"simple_stock_policy_{channel_id}"
        stock_terms_error_key = f"simple_stock_terms_error_{channel_id}"
        language_key = f"simple_language_{channel_id}"
        fact_policy_key = f"simple_fact_policy_{channel_id}"
        quality_policy_key = f"simple_savage_quality_{channel_id}"
        duration_key = f"simple_duration_{channel_id}"
        style_key = f"simple_style_{channel_id}"
        narrator_key = f"simple_narrator_{channel_id}"
        profanity_key = f"simple_profanity_{channel_id}"
        narrator_choices = {
            "Desi Savage Hindi": "desi_savage_aggressive",
            "Standard": "standard",
        }
        profanity_choices = {
            "Mild": "mild",
            "Savage — Monetization Aware": "savage_monetization_aware",
            "Savage Unfiltered": "savage_unfiltered",
        }
        st.session_state.setdefault(duration_key, profile.target_duration_seconds)
        st.session_state.setdefault(fact_policy_key, "off")
        st.session_state.setdefault(
            quality_policy_key, getattr(profile, "savage_quality_policy", "advisory")
        )
        from webui.autopilot_view import sync_manual_style_state
        sync_manual_style_state(st.session_state, channel_id, profile.version,
                                profile.visual_style, styles,
                                profile.target_duration_seconds,
                                profile.video_aspect.value)
        narrator_default = next(
            (label for label, value in {
                "Desi Savage Hindi": "desi_savage_aggressive",
                "Standard": "standard",
            }.items() if value == profile.narrator_preset),
            "Desi Savage Hindi",
        )
        profanity_default = next(
            (label for label, value in {
                "Mild": "mild",
                "Savage — Monetization Aware": "savage_monetization_aware",
                "Savage Unfiltered": "savage_unfiltered",
            }.items() if value == profile.profanity_level),
            "Savage — Monetization Aware",
        )
        st.session_state.setdefault(narrator_key, narrator_default)
        st.session_state.setdefault(profanity_key, profanity_default)
        source_params = edited.get("params_snapshot", {}) if isinstance(edited, dict) else {}
        saved_terms = source_params.get("video_terms", "")
        if isinstance(saved_terms, list):
            saved_terms = ", ".join(str(term) for term in saved_terms)
        st.session_state.setdefault(stock_terms_key, str(saved_terms or ""))
        st.session_state.setdefault(
            stock_policy_key,
            source_params.get("stock_media_policy") or profile.stock_media_policy,
        )

        def current_draft_profile():
            language_label = st.session_state.get(
                language_key, "Hindi" if profile.language.lower().startswith("hi") else "English")
            style_label = st.session_state.get(style_key, "")
            selected_style = styles.get(style_label, profile.visual_style)
            style_defaults = visual_style_service.preset_for(selected_style).defaults
            source_settings = edited.get("params_snapshot", {}) if isinstance(edited, dict) else {}
            inherited_fields = {
                key: source_settings[key] for key in (
                    "voice_name", "tts_model", "voice_rate", "voice_volume",
                    "subtitle_enabled", "subtitle_position", "subtitle_display_mode",
                    "subtitle_animation", "duration_enforcement", "hindi_alignment_policy",
                    "bgm_type", "bgm_file", "bgm_volume", "stock_video_style",
                    "video_source", "asmr_categories", "allow_unverified_publication",
                ) if key in source_settings
            }
            generation_settings = dict(profile.generation_settings)
            if source_settings.get("tts_provider"):
                generation_settings["tts_provider"] = source_settings["tts_provider"]
            tts_defaults = {}
            if (selected_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER
                    and config.app.get("gemini_api_key")):
                tts_defaults = {
                    "voice_name": style_defaults["voice_name"],
                    "tts_model": style_defaults["tts_model"],
                }
            for key in ("voice_name", "tts_model"):
                if key in source_settings:
                    tts_defaults[key] = source_settings[key]
            return profile.model_copy(update={
                **inherited_fields,
                "generation_settings": generation_settings,
                "language": "hi" if language_label == "Hindi" else "en",
                "target_duration_seconds": int(st.session_state.get(duration_key, profile.target_duration_seconds)),
                "visual_style": selected_style,
                "fact_verification_policy": st.session_state.get(
                    fact_policy_key, profile.fact_verification_policy
                ),
                "savage_quality_policy": st.session_state.get(
                    quality_policy_key, "advisory"
                ),
                "stock_video_style": style_defaults.get("stock_video_style", "default"),
                "subtitle_display_mode": style_defaults.get(
                    "caption_preset", profile.subtitle_display_mode
                ),
                "subtitle_animation": style_defaults.get(
                    "subtitle_animation", profile.subtitle_animation
                ),
                "narrator_preset": narrator_choices.get(
                    st.session_state.get(narrator_key, "Desi Savage Hindi"),
                    profile.narrator_preset,
                ),
                "profanity_level": profanity_choices.get(
                    st.session_state.get(profanity_key, "Savage — Monetization Aware"),
                    profile.profanity_level,
                ),
                **tts_defaults,
            })

        def draft_script_for_topic(topic_text):
            draft_profile = current_draft_profile()
            provider_id = str(config.app.get("llm_provider", "")).lower()
            provider_spec = llm.get_llm_provider(provider_id)
            provider_model = (
                config.app.get(provider_spec.config_key("model_name"))
                if provider_spec is not None else ""
            )
            provider_base_url = (
                config.app.get(provider_spec.config_key("base_url"))
                if provider_spec is not None else ""
            )
            cache_data = {
                "topic": topic_text.strip(),
                "channel_id": draft_profile.channel_id,
                "niche": draft_profile.niche,
                "script_style": draft_profile.script_style,
                "topic_preferences": draft_profile.topic_preferences,
                "target_duration_seconds": draft_profile.target_duration_seconds,
                "duration_enforcement": draft_profile.duration_enforcement,
                "language": draft_profile.language,
                "visual_style": draft_profile.visual_style,
                "narrator_preset": draft_profile.narrator_preset,
                "profanity_level": draft_profile.profanity_level,
                "savage_quality_policy": draft_profile.savage_quality_policy,
                "provider": provider_id,
                "model": provider_model,
                "base_url": provider_base_url,
            }
            cache_key = hashlib.sha256(json.dumps(
                cache_data, sort_keys=True, ensure_ascii=False
            ).encode("utf-8")).hexdigest()
            cache_slot = f"simple_script_cache_{channel_id}"
            cached = st.session_state.get(cache_slot)
            if isinstance(cached, dict) and cached.get("key") == cache_key:
                return cached["script"]
            generated = service.draft_script(draft_profile, topic_text)
            st.session_state[cache_slot] = {"key": cache_key, "script": generated}
            return generated

        def generate_savage_stock_terms(topic_text, narration):
            draft_profile = current_draft_profile()
            params = channel_profiles.build_preview(draft_profile, {
                "video_subject": topic_text,
                "video_script": narration,
                "visual_style": draft_profile.visual_style,
            }).params
            represented = tm._represent_script(params, narration).spoken_script
            beat_count = len(tm.emotion_storyboard.build(
                represented,
                narrator_preset=params.narrator_preset or "desi_savage_aggressive",
                profanity_level=params.profanity_level or "savage_monetization_aware",
            ))
            terms = _run_llm_read_operation(
                "generate_terms",
                lambda app_config_snapshot: llm.generate_terms(
                    topic_text, narration, amount=max(1, beat_count),
                    match_script_order=True, app_config=app_config_snapshot,
                    stock_video_style=params.stock_video_style,
                ),
            )
            if not isinstance(terms, (list, tuple)):
                raise TypeError(str(terms or "The text provider returned stock keywords in an invalid format."))
            terms = [str(term).strip() for term in terms if str(term).strip()]
            if len(terms) != beat_count:
                raise ValueError(
                    f"Generated {len(terms)} keyword(s) for {beat_count} narration beats. "
                    "Generate again or edit the list so every beat has one keyword."
                )
            return ", ".join(terms)

        def refresh_savage_stock_terms(topic_text, narration):
            try:
                st.session_state[stock_terms_key] = generate_savage_stock_terms(
                    topic_text, narration
                )
                st.session_state.pop(stock_terms_error_key, None)
            except Exception as exc:
                st.session_state[stock_terms_error_key] = str(exc)

        def generate_terms_from_editor():
            narration = str(st.session_state.get(draft_key, ""))
            if not narration.strip():
                st.session_state[stock_terms_error_key] = (
                    "Enter narration before generating stock keywords."
                )
                return
            refresh_savage_stock_terms(
                str(st.session_state.get(topic_key, "")).strip(), narration
            )

        def show_simple_script_quality_warnings(generated_script):
            if current_draft_profile().visual_style != visual_style_service.SAVAGE_ANIMATED_EXPLAINER:
                st.session_state.pop(f"simple_script_warnings_{channel_id}", None)
                return
            quality_warnings = script_quality.savage_script_warnings(generated_script)
            st.session_state[f"simple_script_warnings_{channel_id}"] = {
                "digest": hashlib.sha256(generated_script.strip().encode("utf-8")).hexdigest(),
                "warnings": quality_warnings,
            }

        def show_simple_script_failure(exc):
            st.session_state.pop(f"simple_script_warnings_{channel_id}", None)
            rejected = script_quality.parse_failed_draft(str(exc))
            if rejected:
                rejected_script, issues = rejected
                if rejected_script:
                    st.session_state[draft_key] = rejected_script
                    st.session_state[generated_script_key] = rejected_script
                st.error(
                    "Savage narration was rejected by the selected Strict quality policy. "
                    "The actual draft is loaded in the script editor. "
                    f"Issues: {'; '.join(issues)}"
                )
            else:
                st.error(f"Narration script could not be generated: {exc}")

        def script_can_be_replaced():
            current_script = str(st.session_state.get(draft_key, ""))
            generated_script = str(st.session_state.get(generated_script_key, ""))
            return not current_script.strip() or (generated_script and current_script == generated_script)

        if st.button("✨ Generate an idea with AI", key="simple_idea"):
            idea = ""
            try:
                with st.spinner("Generating an idea and narration script…"):
                    idea = service._topic_for_slot(current_draft_profile(), datetime.now().astimezone())
                    st.session_state[topic_key] = idea
                    if script_can_be_replaced():
                        generated_script = draft_script_for_topic(idea)
                        st.session_state[draft_key] = generated_script
                        st.session_state[generated_script_key] = generated_script
                        show_simple_script_quality_warnings(generated_script)
                        if current_draft_profile().visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER:
                            refresh_savage_stock_terms(idea, generated_script)
                    else:
                        generated_script = ""
            except Exception as exc:
                if idea:
                    st.session_state[topic_key] = idea
                    st.success(f"Idea generated and added to the topic field: {idea}")
                    show_simple_script_failure(exc)
                else:
                    st.error(f"Idea generation failed: {exc}")
            else:
                st.success(f"Idea generated and added to the topic field: {idea}")
                if generated_script:
                    st.success("Narration script generated and added below.")
                else:
                    st.info("Your existing script was kept. Clear it if you want an AI draft for this idea.")

        topic = st.text_area("Topic / video idea", placeholder="ऑक्टोपस के तीन दिल क्यों होते हैं?",
                             height=75, key=topic_key,
                             help="Enter the video idea, then click Generate narration script.")
        generate_script_clicked = st.button("Generate narration script", key=f"simple_script_generate_{channel_id}")
        if generate_script_clicked:
            if not topic.strip():
                st.warning("Enter a topic before generating its narration script.")
            elif not script_can_be_replaced():
                st.info("Your existing script was kept. Clear it first if you want a new AI draft.")
            else:
                try:
                    with st.spinner("Generating narration script…"):
                        generated_script = draft_script_for_topic(topic.strip())
                    st.session_state[draft_key] = generated_script
                    st.session_state[generated_script_key] = generated_script
                    if current_draft_profile().visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER:
                        refresh_savage_stock_terms(topic.strip(), generated_script)
                    st.success("Narration script generated. Review or edit it below.")
                except Exception as exc:
                    show_simple_script_failure(exc)
        topic_too_long = len(topic.strip()) > 500
        if topic_too_long:
            st.warning("This topic is over 500 characters. Keep the idea, not the log output, then Generate Video will be available.")
        cols = st.columns(4)
        duration = cols[0].number_input("Duration (seconds)", min_value=5, max_value=3600,
                                        key=duration_key)
        format_name = cols[1].selectbox("Format", ["Shorts (9:16)", "Landscape (16:9)"],
                                        index=0 if profile.video_aspect.value == "9:16" else 1,
                                        key=f"simple_format_{channel_id}")
        style_name = cols[2].selectbox("Visual style", list(styles),
                                       key=style_key,
                                       on_change=lambda: st.session_state.__setitem__(
                                           f"simple_style_user_selected_{channel_id}", True
                                       ))
        language = cols[3].selectbox("Language", ["Hindi", "English"],
                                     index=0 if profile.language.lower().startswith("hi") else 1,
                                     key=language_key)
        narration_cols = st.columns(2)
        narrator_labels = list(narrator_choices)
        profanity_labels = list(profanity_choices)
        narrator_label = narration_cols[0].selectbox(
            "Narration personality", narrator_labels,
            index=None,
            key=narrator_key,
            help="Independent of Visual Style. Desi Savage Hindi is the default for Facts Sun Le Bhai.",
        )
        profanity_label = narration_cols[1].selectbox(
            "Profanity intensity", profanity_labels,
            index=None,
            key=profanity_key,
            help="Unfiltered profanity may reduce YouTube advertiser suitability.",
        )
        fact_policy = st.selectbox(
            "Fact checking", ["off", "advisory", "strict"],
            format_func={"off": "Off", "advisory": "Advisory", "strict": "Strict"}.get,
            key=fact_policy_key,
            help=("Optional. Off skips research. Advisory checks briefly and continues if evidence is "
                  "unavailable. Strict blocks TTS unless the exact narration is source-verified."),
        )
        background_music_key = f"simple_background_music_{channel_id}"
        st.session_state.setdefault(background_music_key, False)
        include_background_music = st.checkbox(
            "Include background music",
            key=background_music_key,
            help="Optional. Off creates voice-only audio. On uses the background music saved for this channel.",
        )
        edit_branding = (
            edited.get("params_snapshot", {}).get("channel_branding", {})
            if isinstance(edited, dict) and isinstance(edited.get("params_snapshot"), dict)
            else {}
        )
        branding_cols = st.columns(2)
        include_intro = branding_cols[0].checkbox(
            "Include channel intro",
            value=bool(edit_branding.get("intro", {}).get("enabled", profile.intro_enabled)),
            key=f"simple_intro_{channel_id}",
            help="Uses the currently saved intro audio snapshot for this video.",
        )
        include_outro = branding_cols[1].checkbox(
            "Include channel outro",
            value=bool(edit_branding.get("outro", {}).get("enabled", profile.outro_enabled)),
            key=f"simple_outro_{channel_id}",
            help="Uses the currently saved outro audio snapshot for this video.",
        )
        quality_policy = st.selectbox(
            "Savage creative-quality enforcement", ["advisory", "strict"],
            format_func={"advisory": "Advisory", "strict": "Strict"}.get,
            key=quality_policy_key,
            help=("Advisory keeps coherent drafts and shows creative warnings. Strict rejects "
                  "drafts that still have creative warnings after one rewrite."),
        )
        active_profile = current_draft_profile()
        active_settings = generation_settings_service.resolve_generation_settings(
            channel_profile=active_profile
        )
        st.caption(
            "Effective preset: "
            f"{active_settings.effective_visual_style.replace('_', ' ')} · "
            f"{active_settings.narrator_preset.replace('_', ' ')} · "
            f"{active_settings.profanity_level.replace('_', ' ')} · "
            f"{active_settings.caption_preset.replace('_', ' ')} · "
            f"{active_settings.tts_provider or 'configured voice'} / "
            f"{active_settings.voice_name or 'configured voice'} · "
            f"preset v{active_settings.style_preset_version}"
        )
        from webui.autopilot_view import reset_manual_style_defaults
        if st.button("Reset to Style Defaults", key=f"simple_reset_style_{channel_id}"):
            reset_manual_style_defaults(
                st.session_state, channel_id, active_settings.effective_visual_style
            )
            st.rerun()
        script = st.text_area("Narration script", height=150,
                              key=draft_key,
                              help="Review or edit the generated narration. Your edits are preserved and used as entered.")
        if active_settings.effective_visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER:
            stock_policy = st.selectbox(
                "Stock footage eligibility", ("manual_review", "auto_eligible"),
                key=stock_policy_key,
                format_func={
                    "manual_review": "Manual review",
                    "auto_eligible": "Automatic provider search match (no approval)",
                }.get,
            )
            if stock_policy == "auto_eligible":
                provider_order = material.stock_video_provider_order(profile.video_source)
                scope = " → ".join(item.title() for item in provider_order) or "no configured stock provider"
                st.caption(f"Effective stock policy: Automatic · provider order: {scope}. Search relevance is not visual verification.")
            else:
                st.caption("Effective stock policy: Manual · only hash-approved clips can render.")
            st.text_area(
                "Stock video keywords", key=stock_terms_key,
                help=("One English stock search keyword per narration beat, in order. "
                      "Edit or repeat a keyword when consecutive beats cover the same topic."),
            )
            if stock_policy == "manual_review":
                _render_stock_policy_preflight(
                    stock_policy, st.session_state.get(stock_terms_key, ""),
                    profile.video_source,
                )
            st.button(
                "Generate stock keywords from narration",
                key=f"simple_stock_terms_generate_{channel_id}",
                on_click=generate_terms_from_editor,
            )
            if st.session_state.get(stock_terms_error_key):
                st.warning(f"Stock keywords need attention: {st.session_state[stock_terms_error_key]}")
        quality_review = st.session_state.get(f"simple_script_warnings_{channel_id}")
        if (isinstance(quality_review, dict)
                and quality_review.get("digest") == hashlib.sha256(
                    script.strip().encode("utf-8")
                ).hexdigest()
                and quality_review.get("warnings")):
            st.warning(
                "Savage creative-quality notes (Advisory): "
                + "; ".join(quality_review["warnings"])
            )
        if st.session_state.get(f"simple_script_needs_style_review_{channel_id}"):
            st.info(
                "Your narration was kept after the style change. Generate a new narration "
                "explicitly if you want it rewritten for this preset."
            )
        generate_help = (
            "Shorten the topic to 500 characters or less." if topic_too_long else
            "Enter a topic or narration script, and enable this channel first."
            if not (topic.strip() or script.strip()) or not profile.enabled else None
        )
        if st.button("Generate Video", type="primary",
                     disabled=not (topic.strip() or script.strip()) or topic_too_long or not profile.enabled,
                     help=generate_help,
                     key="simple_generate"):
            job_topic = _manual_video_topic(topic, script)
            with st.spinner("Preparing your script and starting video generation…"):
                try:
                    job = _autopilot_create_video(profile, job_topic, script, duration,
                        "9:16" if format_name.startswith("Shorts") else "16:9", styles[style_name],
                        "hi" if language == "Hindi" else "en",
                        narrator_preset=narrator_choices[narrator_label],
                        profanity_level=profanity_choices[profanity_label],
                        fact_verification_policy=fact_policy,
                        savage_quality_policy=quality_policy,
                        background_music_enabled=include_background_music,
                        review_ai_draft=False,
                        video_terms=(
                            st.session_state.get(stock_terms_key, "")
                            if active_settings.effective_visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER
                            else None
                        ),
                        stock_media_policy=(
                            st.session_state.get(stock_policy_key)
                            if active_settings.effective_visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER
                            else None
                        ),
                        source_settings=(edited.get("params_snapshot", {}) if isinstance(edited, dict) else None),
                        parent_job_id=(edited.get("job_id") if isinstance(edited, dict) else None),
                        intro_enabled=include_intro, outro_enabled=include_outro)
                    if job:
                        st.session_state["autopilot_active_job"] = job.job_id
                        st.success("Video job created and queued.")
                        if isinstance(edited, dict):
                            st.session_state.pop("autopilot_edit_job", None)
                            st.session_state.pop("autopilot_edit_loaded_source_id", None)
                    else:
                        st.info("Review the AI draft and its estimated duration below, then choose whether to render it.")
                except autopilot_service.DuplicateContent:
                    audio_retry = service.failed_manual_job(channel_id, job_topic, script)
                    if audio_retry:
                        try:
                            replacement = service.resume_manual_job(audio_retry.job_id)
                            st.session_state["autopilot_active_job"] = replacement.job_id
                            st.success("Your failed video attempt was resumed. Follow its progress below or in My Videos.")
                        except Exception as exc:
                            st.error(f"A new attempt for this failed video could not start: {exc}")
                    else:
                        try:
                            resumed = _autopilot_resume_draft(profile, job_topic, script, duration,
                                "9:16" if format_name.startswith("Shorts") else "16:9", styles[style_name],
                                "hi" if language == "Hindi" else "en",
                                narrator_preset=narrator_choices[narrator_label],
                                profanity_level=profanity_choices[profanity_label],
                                fact_verification_policy=fact_policy,
                                savage_quality_policy=quality_policy,
                                background_music_enabled=include_background_music,
                                intro_enabled=include_intro, outro_enabled=include_outro,
                                review_ai_draft=False,
                                video_terms=(
                                    st.session_state.get(stock_terms_key, "")
                                    if active_settings.effective_visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER
                                    else None
                                ), stock_media_policy=(
                                    st.session_state.get(stock_policy_key)
                                    if active_settings.effective_visual_style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER
                                    else None
                                ))
                            if resumed is False:
                                st.info("An existing draft was found. Review its script and duration below before rendering.")
                            elif resumed:
                                st.session_state["autopilot_active_job"] = resumed.job_id
                                st.success("Existing draft queued with your selected channel settings.")
                            else:
                                st.warning("This topic is already in this channel's history. Open My Videos or change the angle of the idea before creating another version. Your script was kept.")
                        except Exception as exc:
                            st.error(f"An existing draft was found, but generation could not start: {exc}")
                except Exception as exc:
                    error_text = str(exc).strip()
                    if not error_text or error_text.casefold() == "none":
                        error_text = (
                            f"{type(exc).__name__} (no error detail returned). "
                            "Check the active job status below or in My Videos."
                        )
                    st.error(f"Video could not be created: {error_text}")
        active_job_id = st.session_state.get("autopilot_active_job")
        if active_job_id:
            _render_autopilot_active_job(active_job_id, channel_id)
        pending_id = st.session_state.get("autopilot_pending_plan")
        if pending_id:
            try:
                pending = service.store.get_plan(pending_id, channel_id)
                if pending.state in {"planned", "ready"}:
                    st.text_area("Review AI script", pending.script, height=150, disabled=True)
                    st.caption(f"Estimated narration: {pending.duration_estimate_seconds:.0f} seconds. Target: {pending.min_duration_seconds:.0f}–{pending.max_duration_seconds:.0f} seconds.")
                    if st.button("Use this draft and render", key="simple_accept_draft"):
                        try:
                            if pending.state == "planned":
                                service.ready(channel_id, pending.plan_id, True)
                            job = service.create_job(
                                channel_id, pending.plan_id,
                                video_terms=st.session_state.get("autopilot_pending_video_terms"),
                            )
                            st.session_state["autopilot_last_job"] = job.job_id
                            st.session_state["autopilot_active_job"] = job.job_id
                            service.start_job(job.job_id)
                            st.session_state.pop("autopilot_pending_plan", None)
                            st.session_state.pop("autopilot_pending_video_terms", None)
                            st.success("Video job created and queued. Open My Videos to follow progress.")
                        except Exception as exc:
                            st.error(f"Draft could not be rendered: {exc}")
            except LookupError:
                st.session_state.pop("autopilot_pending_plan", None)

    if videos_tab.open:
        _render_autopilot_videos(channel_id)

    if schedule_tab.open:
        st.subheader("Schedule & Automation")
        state = service.schedule_state(channel_id)
        with st.form(f"simple_schedule_{channel_id}"):
            enabled = st.toggle("Enable recurring videos", value=bool(profile.publications_per_week and not state["paused"]))
            frequency = st.number_input("Videos per week", 1, 21, value=max(1, min(profile.publications_per_week, 21)))
            publish_times = st.text_input(
                "Local publish times (HH:MM, comma separated)",
                value=", ".join(profile.schedule_times_local),
                help="For three daily Shorts, use 21 per week, three times such as 09:00,14:00,20:00, and Asia/Kolkata.",
            )
            timezone_name = st.text_input("Timezone", profile.timezone)
            review = st.radio("Publishing", ["Review before upload", "Automatically upload privately"],
                              index=0 if profile.publishing_policy == "manual" else 1)
            st.caption("Automatic public publishing stays off. Add topic preferences in Channels for scheduled ideas.")
            if st.form_submit_button("Save schedule"):
                try:
                    _autopilot_save_profile(profile, publications_per_week=int(frequency) if enabled else 0,
                        schedule_times_local=[item.strip() for item in publish_times.split(",") if item.strip()], schedule_weekdays=list(range(7)),
                        timezone=timezone_name, publishing_policy="manual" if review.startswith("Review") else "private",
                        auto_public_enabled=False)
                    service.set_channel_paused(channel_id, not enabled, "Changed in Channel Autopilot")
                    st.success("Schedule saved.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Schedule could not be saved: {exc}")
        slots = service.store.list_slots(channel_id)
        buffer_state = state["buffer"]
        st.caption(
            f"Ready buffer: {buffer_state['validated']}/{buffer_state['target']} validated · "
            f"{buffer_state['in_progress']} generating or queued"
        )
        upcoming = _autopilot_upcoming_schedule(profile) if profile.publications_per_week and not state["paused"] else []
        if upcoming:
            st.markdown("**Upcoming scheduled jobs**")
            for slot in upcoming:
                st.write(slot.strftime("%a %d %b, %I:%M %p %Z"))
        if slots:
            with st.expander("Recent schedule activity"):
                for slot in slots[:10]:
                    style = slot.get("visual_style") or profile.visual_style
                    st.write(f"{slot['slot_utc']} · {slot['state']} · {style}")
                    if slot.get("details"):
                        st.caption(slot["details"])

    if channels_tab.open:
        st.subheader("Channels")
        with st.expander("Add a YouTube channel"):
            with st.form("simple_add_channel"):
                new_name = st.text_input("Channel name")
                new_language = st.selectbox("Narration language", ["Hindi", "English"])
                if st.form_submit_button("Create channel profile"):
                    try:
                        from app.models.channel_profile import ChannelProfile
                        created = channel_profiles.store.create(ChannelProfile(
                            display_name=new_name.strip(), language="hi" if new_language == "Hindi" else "en",
                            publishing_backend="youtube_direct", publishing_policy="manual",
                        ))
                        st.session_state["autopilot_select_after_create"] = created.channel_id
                        st.success("Channel profile created. Connect its YouTube identity next.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Channel could not be created: {exc}")
        for item in profiles:
            with st.container(border=True):
                st.markdown(f"**{item.display_name}** · {_autopilot_connection_label(item, service)}")
                if item.channel_id == channel_id:
                    st.markdown("#### Channel Overview")
                    connection_state = (
                        service.youtube_connections.connection_for_channel(item.channel_id)
                        if item.publishing_backend == "youtube_direct" else None
                    )
                    connected = (
                        bool(connection_state and connection_state.get("state") == "connected")
                        if item.publishing_backend == "youtube_direct"
                        else bool(item.upload_post_username)
                    )
                    st.caption(
                        f"{item.display_name} · "
                        f"{'Connected' if connected else 'Connection needed'} · "
                        f"Autopilot {'enabled' if item.enabled else 'disabled'}"
                    )
                    saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
                    saved_voice_name = config.ui.get("voice_name", "")
                    gemini_voices = voice.get_gemini_voices() if saved_tts_server == "gemini-tts" else []
                    profile_voice_is_gemini = voice.is_gemini_voice(item.voice_name)
                    if gemini_voices and not profile_voice_is_gemini:
                        st.warning(
                            "This channel profile still has an older non-Gemini voice. "
                            f"The Video Generator is set to {saved_voice_name or 'a Gemini voice'}. "
                            "Choose that Gemini voice below and save channel defaults before retrying."
                        )
                    with st.form(f"simple_channel_settings_{channel_id}"):
                        st.markdown("**Content & Narration**")
                        if gemini_voices:
                            preferred_voice = saved_voice_name if saved_voice_name in gemini_voices else gemini_voices[0]
                            initial_voice = item.voice_name if item.voice_name in gemini_voices else preferred_voice
                            voice_name = st.selectbox(
                                "Narration voice",
                                gemini_voices,
                                index=gemini_voices.index(initial_voice),
                                help="Uses the Gemini TTS provider and API key saved in Video Generator settings.",
                            )
                            st.caption("The channel profile stores its own voice. Saving here applies it to manual and scheduled videos for this channel.")
                        else:
                            voice_name = st.text_input("Narration voice", item.voice_name,
                                help="Voice ID used by this channel's video jobs.")
                        channel_language = st.selectbox("Default language", ["Hindi", "English"],
                            index=0 if item.language.lower().startswith("hi") else 1)
                        caption_language = st.selectbox(
                            "YouTube caption language", ["English", "Hindi"],
                            index=0 if item.youtube_caption_language == "English" else 1,
                            help="Controls generated YouTube titles and captions independently of narration language.",
                        )
                        channel_style = st.selectbox("Default visuals", list(styles),
                            index=list(styles.values()).index(item.visual_style))
                        channel_hashtag = st.text_input(
                            "YouTube channel hashtag", item.channel_hashtag,
                            help="Optional brand hashtag included with the five caption hashtags. Enter it with or without #.",
                        )
                        preferences = st.text_area("Topic preferences (one per line)", "\n".join(item.topic_preferences))
                        if st.form_submit_button("Save channel defaults"):
                            try:
                                _autopilot_save_profile(item, voice_name=voice_name,
                                    language="hi" if channel_language == "Hindi" else "en",
                                    youtube_caption_language=caption_language,
                                    visual_style=styles[channel_style],
                                    channel_hashtag=channel_hashtag.strip(),
                                    topic_preferences=[line.strip() for line in preferences.splitlines() if line.strip()])
                                st.success("Channel defaults saved.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Channel settings could not be saved: {exc}")
                    st.markdown("#### Channel Branding — Intro & Outro")
                    from app.services import channel_branding as branding_service
                    branding_preview = channel_profiles.build_preview(item).params
                    with st.container(border=True):
                        image_upload_col, image_preview_col = st.columns([1.5, 1])
                        with image_upload_col:
                            st.markdown("##### Outro profile image")
                            profile_image_upload = st.file_uploader(
                                "Optional channel profile image for the outro",
                                type=["png", "jpg", "jpeg", "webp"],
                                key=f"channel_branding_profile_image_{channel_id}",
                                help="PNG, JPG, or WebP · up to 10 MB. The image appears in the outro's top visual panel.",
                            )
                            if profile_image_upload and st.button(
                                "Save image",
                                key=f"channel_branding_profile_image_save_{channel_id}",
                            ):
                                try:
                                    current_image = item.profile_image or {}
                                    image_asset = branding_service.save_profile_image(
                                        channel_id, profile_image_upload.getvalue(),
                                        version=int(current_image.get("version", 0)) + 1,
                                    )
                                    _autopilot_save_profile(
                                        item, profile_image=image_asset,
                                        **({"outro_video": None} if item.outro_video else {}),
                                    )
                                    st.success("Image saved. Generate a new outro video to include it.")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Channel profile image could not be saved: {exc}")
                        with image_preview_col:
                            if item.profile_image:
                                try:
                                    image_path = branding_service.resolve_profile_image(
                                        channel_id, item.profile_image.get("asset_key", ""),
                                    )
                                    if hashlib.sha256(image_path.read_bytes()).hexdigest() != item.profile_image.get("content_hash"):
                                        raise ValueError("saved image hash does not match")
                                    from PIL import Image
                                    with Image.open(image_path) as saved_image:
                                        saved_image.thumbnail((320, 320))
                                        image_preview = saved_image.copy()
                                    st.image(image_preview, width=180, caption="Saved image")
                                except (ValueError, OSError, TypeError):
                                    st.warning("Saved channel profile image is missing or invalid.")
                            else:
                                st.caption("No profile image saved")
                    for segment in ("intro", "outro"):
                        segment_audio = getattr(item, f"{segment}_audio") or {}
                        segment_video = getattr(item, f"{segment}_video") or {}
                        with st.expander(segment.capitalize(), expanded=False):
                            enabled = st.toggle(
                                f"Enable {segment}",
                                value=getattr(item, f"{segment}_enabled"),
                                key=f"channel_branding_{segment}_enabled_{channel_id}",
                            )
                            spoken_text = st.text_area(
                                "Spoken text", value=getattr(item, f"{segment}_text"),
                                max_chars=1000,
                                help="Hindi, Hinglish (Devanagari or Latin script), or English. The saved channel voice will speak this text as entered.",
                                key=f"channel_branding_{segment}_text_{channel_id}",
                            )
                            save_branding = st.button(
                                "Save branding settings",
                                key=f"channel_branding_{segment}_save_{channel_id}",
                            )
                            uploaded_branding = st.file_uploader(
                                "Upload custom MP3/WAV", type=["mp3", "wav"],
                                key=f"channel_branding_{segment}_upload_{channel_id}",
                            )
                            uploaded_subtitles = st.file_uploader(
                                "Matching timed subtitles (SRT, required for a video from uploaded audio)",
                                type=["srt"],
                                key=f"channel_branding_{segment}_subtitle_upload_{channel_id}",
                            )
                            force_regeneration = st.checkbox(
                                "Regenerate matching audio",
                                key=f"channel_branding_{segment}_force_{channel_id}",
                                help="Normally matching saved audio is reused without another TTS request.",
                            )
                            if save_branding:
                                try:
                                    _autopilot_save_profile(
                                        item, **{f"{segment}_enabled": enabled,
                                                 f"{segment}_text": spoken_text.strip()}
                                    )
                                    st.success(f"{segment.capitalize()} settings saved.")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Branding settings could not be saved: {exc}")
                            if st.button(
                                "Generate Audio", key=f"channel_branding_{segment}_generate_{channel_id}",
                                disabled=not spoken_text.strip(),
                            ):
                                generated_path = None
                                try:
                                    from app.services.generation_settings import resolve_task_settings
                                    settings = resolve_task_settings(
                                        branding_preview, topic=item.display_name
                                    )
                                    request_hash = hashlib.sha256(json.dumps({
                                        "text": spoken_text.strip(),
                                        "voice": branding_preview.voice_name,
                                        "rate": branding_preview.voice_rate,
                                        "provider": settings.tts_provider,
                                        "model": settings.tts_model,
                                    }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
                                    if (segment_audio.get("request_hash") == request_hash
                                            and not force_regeneration):
                                        st.info("Matching saved audio already exists; reused it without another TTS request.")
                                        _autopilot_save_profile(
                                            item, **{f"{segment}_text": spoken_text.strip(),
                                                     f"{segment}_enabled": enabled}
                                        )
                                    else:
                                        with tempfile.TemporaryDirectory(prefix="mpt-branding-") as tmp:
                                            generated_path = os.path.join(tmp, f"{segment}.mp3")
                                            maker = voice.tts(
                                                text=spoken_text.strip(),
                                                voice_name=voice.parse_voice_name(branding_preview.voice_name),
                                                voice_rate=branding_preview.voice_rate,
                                                voice_file=generated_path,
                                                tts_model=settings.tts_model,
                                                tts_provider=settings.tts_provider,
                                            )
                                            if maker is None or not os.path.isfile(generated_path):
                                                raise RuntimeError("TTS did not produce a usable audio file")
                                            cue_path = os.path.join(tmp, f"{segment}.srt")
                                            voice.create_subtitle(
                                                text=spoken_text.strip(), sub_maker=maker,
                                                subtitle_file=cue_path, word_level=False,
                                            )
                                            cues = Path(cue_path).read_bytes() if os.path.isfile(cue_path) else None
                                            next_version = int(segment_audio.get("version", 0)) + 1
                                            saved_asset = branding_service.save_asset(
                                                channel_id, segment, Path(generated_path).read_bytes(),
                                                extension="mp3", version=next_version,
                                                provider=settings.tts_provider, model=settings.tts_model,
                                                voice_name=branding_preview.voice_name,
                                                request_hash=request_hash, cues=cues,
                                            )
                                        _autopilot_save_profile(
                                            item, **{f"{segment}_audio": saved_asset,
                                                     f"{segment}_text": spoken_text.strip(),
                                                     f"{segment}_enabled": enabled,
                                                     f"{segment}_video": None}
                                        )
                                        st.success(f"{segment.capitalize()} audio saved and ready to reuse.")
                                        st.rerun()
                                except Exception as exc:
                                    st.error(f"{segment.capitalize()} audio could not be generated: {exc}")
                            if uploaded_branding:
                                if st.button(
                                    "Save uploaded audio",
                                    key=f"channel_branding_{segment}_upload_save_{channel_id}",
                                ):
                                    try:
                                        next_version = int(segment_audio.get("version", 0)) + 1
                                        saved_asset = branding_service.save_asset(
                                            channel_id, segment, uploaded_branding.getvalue(),
                                            extension=Path(uploaded_branding.name).suffix.removeprefix(".").lower(),
                                            version=next_version,
                                            cues=uploaded_subtitles.getvalue() if uploaded_subtitles else None,
                                        )
                                        _autopilot_save_profile(
                                            item, **{f"{segment}_audio": saved_asset,
                                                     f"{segment}_text": spoken_text.strip(),
                                                     f"{segment}_enabled": enabled,
                                                     f"{segment}_video": None}
                                        )
                                        st.success(f"Uploaded {segment} audio saved.")
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"Uploaded audio could not be saved: {exc}")
                            if segment_audio:
                                try:
                                    saved_path = branding_service.resolve_asset(
                                        channel_id, segment_audio.get("asset_key"),
                                        expected_kind=segment,
                                    )
                                    if (hashlib.sha256(saved_path.read_bytes()).hexdigest()
                                            != segment_audio.get("content_hash")
                                            or voice.get_audio_duration(str(saved_path)) <= 0):
                                        raise ValueError("saved audio failed validation")
                                    mime = "audio/wav" if saved_path.suffix == ".wav" else "audio/mpeg"
                                    st.audio(saved_path.read_bytes(), format=mime)
                                    st.caption(
                                        f"Ready · {segment_audio.get('duration_seconds', 0):.2f}s · "
                                        f"v{segment_audio.get('version', '?')}"
                                    )
                                except (ValueError, OSError, TypeError):
                                    st.warning("Enabled branding audio is missing or invalid. Upload or regenerate it to repair.")
                                ratio_options = ["Vertical (9:16)", "Horizontal (16:9)"]
                                saved_ratio = segment_video.get("aspect")
                                ratio_index = (0 if saved_ratio == "9:16" else 1
                                               if saved_ratio == "16:9" else None)
                                ratio_col, action_col = st.columns([1, 1])
                                with ratio_col:
                                    selected_ratio = st.selectbox(
                                        "Video ratio · required",
                                        ratio_options,
                                        index=ratio_index,
                                        key=f"channel_branding_{segment}_video_ratio_{channel_id}",
                                        help="Choose the shape of the saved intro/outro video.",
                                    )
                                with action_col:
                                    action_label = "Regenerate video" if segment_video else "Generate video"
                                    generate_video_clicked = st.button(
                                        action_label,
                                        key=f"channel_branding_{segment}_video_generate_{channel_id}",
                                        disabled=selected_ratio is None,
                                        help="Uses the saved audio and timed subtitles. Does not make a TTS request.",
                                    )
                                if generate_video_clicked:
                                    try:
                                        with tempfile.TemporaryDirectory(prefix="mpt-brand-video-") as tmp:
                                            output_path = os.path.join(tmp, f"{segment}.mp4")
                                            video_asset = branding_service.generate_video(
                                                channel_id, segment, segment_audio,
                                                display_name=item.display_name,
                                                aspect=("9:16" if selected_ratio.startswith("Vertical") else "16:9"),
                                                profile_image=item.profile_image if segment == "outro" else None,
                                                output_path=output_path,
                                                version=int(segment_video.get("version", 0)) + 1,
                                            )
                                        _autopilot_save_profile(
                                            item, **{f"{segment}_video": video_asset}
                                        )
                                        st.success(f"{segment.capitalize()} video saved ({video_asset['aspect']}) without another TTS request.")
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"{segment.capitalize()} video could not be generated: {exc}")
                                if segment_video:
                                    try:
                                        video_path = branding_service.resolve_video(
                                            channel_id, segment_video.get("asset_key", ""),
                                            expected_kind=segment,
                                        )
                                        if hashlib.sha256(video_path.read_bytes()).hexdigest() != segment_video.get("content_hash"):
                                            raise ValueError("saved video hash does not match")
                                        if segment_video.get("layout_version") != 2:
                                            st.warning("This video uses the previous avatar layout. Regenerate it to match the final video's character position.")
                                        else:
                                            with st.expander("Preview saved video", expanded=False):
                                                st.video(str(video_path), width=420)
                                            st.caption(
                                                f"Saved · {'Vertical 9:16' if segment_video.get('aspect') == '9:16' else 'Horizontal 16:9'}"
                                                " · matches saved audio and timed subtitles"
                                            )
                                    except (ValueError, OSError, TypeError):
                                        st.warning("Saved branding video is missing or no longer valid. Generate it again.")
                                else:
                                    st.caption(
                                        "Audio will play with the selected visuals until you generate a matching branded video."
                                    )
                            else:
                                st.caption("No saved audio. Intro/outro is optional and disabled by default.")
                    if item.publishing_backend == "youtube_direct":
                        st.markdown("#### Publishing Settings")
                        connection = service.youtube_connections.connection_for_channel(item.channel_id)
                        st.caption(f"Connection: {connection.get('state') if connection else 'not connected'}"
                                   + (f" · Last updated {connection['updated_at'][:10]}" if connection else ""))
                        if connection and st.button("Check token health", key=f"simple_health_{channel_id}"):
                            try:
                                service.youtube_publisher.access_token(connection["connection_id"])
                                st.success("YouTube connection is ready.")
                            except Exception as exc:
                                st.error(f"YouTube needs reconnection: {exc}")
                        if st.button("Connect / reconnect YouTube", key=f"simple_connect_{channel_id}"):
                            try:
                                _, url = service.youtube_connections.begin_oauth(channel_id)
                                st.session_state[f"simple_oauth_url_{channel_id}"] = url
                            except Exception as exc:
                                st.error(f"Connection could not start: {exc}")
                        if st.session_state.get(f"simple_oauth_url_{channel_id}"):
                            st.link_button("Continue with Google", st.session_state[f"simple_oauth_url_{channel_id}"])
                        if connection and st.button("Disconnect YouTube", key=f"simple_disconnect_{channel_id}"):
                            try:
                                service.youtube_publisher.revoke(connection["connection_id"])
                            except Exception as exc:
                                st.warning(f"Local connection removed; Google revocation status: {exc}")
                            _autopilot_save_profile(item, youtube_connection_id=None, youtube_channel_id=None)
                            st.rerun()

    if advanced_tab.open:
        st.session_state["autopilot_profile_channel_select"] = channel_id
        _render_channel_autopilot_advanced()


def _render_channel_autopilot_advanced():
    """Manage channel profiles, scheduled production, QA and publication receipts."""
    st.title("Channel Autopilot")
    st.caption("Schedules and immutable jobs are stored in SQLite.")
    profiles = channel_profiles.store.list()
    selected_after_save = st.session_state.pop("autopilot_select_after_save", None)
    if selected_after_save in {profile.channel_id for profile in profiles}:
        st.session_state["autopilot_profile_channel_select"] = selected_after_save
    profile_by_id = {profile.channel_id: profile for profile in profiles}
    selected_channel_id = st.selectbox(
        "Channel profile",
        list(profile_by_id),
        index=None,
        placeholder="Select a channel profile or create a new one",
        format_func=lambda channel_id: (
            f"{profile_by_id[channel_id].display_name} · v{profile_by_id[channel_id].version}"
        ),
        key="autopilot_profile_channel_select",
    )
    current = profile_by_id.get(selected_channel_id)

    with st.form("autopilot_profile_form"):
        display_name = st.text_input("Display name", value=current.display_name if current else "")
        niche = st.text_input("Content niche", value=current.niche if current else "")
        channel_hashtag = st.text_input(
            "YouTube channel hashtag (optional)",
            value=current.channel_hashtag if current else "",
            help="This brand hashtag is included in generated YouTube Shorts captions.",
        )
        # These defaults are edited in the Channels section; don't show a second
        # independent widget for the same profile fields here.
        topics = "\n".join(current.topic_preferences) if current else ""
        language = current.language if current else ""
        script_style = st.text_area("Storytelling style", value=current.script_style if current else "")
        duration = st.number_input("Target duration (seconds)", 5, 3600, current.target_duration_seconds if current else 30)
        duration_modes = ["flexible", "warn_only", "strict"]
        duration_enforcement = st.selectbox(
            "Duration enforcement", duration_modes,
            index=duration_modes.index(current.duration_enforcement if current else "flexible"),
        )
        fact_policy_values = ["off", "advisory", "strict"]
        fact_verification_policy = st.selectbox(
            "Default fact-checking mode", fact_policy_values,
            format_func={"off": "Off", "advisory": "Advisory", "strict": "Strict"}.get,
            index=(fact_policy_values.index(current.fact_verification_policy)
                   if current else fact_policy_values.index("advisory")),
            help="Independent of Visual Style. Advisory continues if sources are unavailable; Strict blocks TTS unless verified.",
        )
        aspect_values = ["9:16", "16:9", "1:1"]
        aspect = st.selectbox("Aspect ratio", aspect_values, index=aspect_values.index(current.video_aspect.value) if current else 0)
        visual_style_value = current.visual_style if current else "savage_animated_explainer"
        narrator_preset = st.selectbox(
            "Narration personality",
            ["desi_savage_aggressive", "standard"],
            index=["desi_savage_aggressive", "standard"].index(
                current.narrator_preset if current else visual_style_service.preset_for(
                    visual_style_value
                ).defaults["narrator_preset"]
            ),
            format_func={
                "desi_savage_aggressive": "Desi Savage Hindi",
                "standard": "Standard",
            }.get,
            help="Independent from Visual Style. Desi Savage Hindi is the default channel voice.",
        )
        profanity_level = st.selectbox(
            "Profanity intensity",
            ["mild", "savage_monetization_aware", "savage_unfiltered"],
            index=["mild", "savage_monetization_aware", "savage_unfiltered"].index(
                current.profanity_level if current else visual_style_service.preset_for(
                    visual_style_value
                ).defaults["profanity_level"]
            ),
            format_func={
                "mild": "Mild",
                "savage_monetization_aware": "Savage — Monetization Aware",
                "savage_unfiltered": "Savage Unfiltered",
            }.get,
            help="Repeated strong profanity may reduce YouTube advertiser suitability.",
        )
        category_values = ["kinetic_sand", "soap", "foam", "jelly", "crystal", "lava_cube"]
        categories = st.multiselect("ASMR categories", category_values, default=current.asmr_categories if current else ["kinetic_sand", "soap"])
        source_values = ["pexels", "pixabay", "coverr", "local", "wavespeed", "volcengine_seedance", "ofox", "metaso_minimax", "muapi", "loomloom", "openai_image"]
        source = st.selectbox("Video source", source_values, index=source_values.index(current.video_source) if current else 0)
        voice_name = current.voice_name if current else str(config.ui.get("voice_name", "") or "")
        voice_rate = st.slider("Voice rate", 0.5, 2.0, float(current.voice_rate if current else 1.0))
        voice_volume = st.slider("Voice volume", 0.0, 2.0, float(current.voice_volume if current else 1.0))
        subtitle = st.checkbox("Subtitles enabled", value=current.subtitle_enabled if current else True)
        subtitle_position = st.selectbox("Subtitle position", ["bottom", "top", "center", "two_thirds_bottom", "custom"], index=["bottom", "top", "center", "two_thirds_bottom", "custom"].index(current.subtitle_position) if current and current.subtitle_position in ["bottom", "top", "center", "two_thirds_bottom", "custom"] else 0)
        subtitle_mode = st.selectbox("Subtitle display", ["sentence", "word_by_word"], index=["sentence", "word_by_word"].index(current.subtitle_display_mode) if current else 0)
        hindi_alignment_policy = st.selectbox(
            "Hindi alignment policy", ["balanced", "strict"],
            index=["balanced", "strict"].index(current.hindi_alignment_policy if current else "balanced"),
            key="channel_hindi_alignment_policy",
        )
        subtitle_animation = st.selectbox("Subtitle animation", ["none", "pop_spring"], index=["none", "pop_spring"].index(current.subtitle_animation) if current else 0)
        font_name = st.text_input("Subtitle font", value=current.font_name if current else "NotoSansDevanagari-VF.ttf")
        bgm_type = st.text_input("Background music type", value=current.bgm_type if current else "random")
        bgm_file = st.text_input("Background music file", value=current.bgm_file if current else "")
        bgm_volume = st.slider("Background music volume", 0.0, 1.0, float(current.bgm_volume if current else 0.2))
        provider = st.text_input("Generation provider setting", value=current.generation_provider if current else "global", help="Identifier only; provider credentials remain in MPT configuration.")
        provider_settings_text = st.text_area("Non-secret provider options (JSON)", value=json.dumps(current.generation_settings if current else {}, ensure_ascii=False), help="Credential-like fields are rejected.")
        timezone_name = st.text_input("Timezone (IANA)", value=current.timezone if current else "UTC")
        frequency = st.number_input("Planned publications per week", 0, 100, current.publications_per_week if current else 0)
        schedule_times = st.text_input("Local publish times (HH:MM, comma separated)", value=", ".join(current.schedule_times_local) if current else "09:00", help="Evaluated in the channel timezone.")
        rotation_styles = st.multiselect(
            "Visual styles to rotate", list(VISUAL_STYLE_LABELS),
            default=(current.visual_style_rotation or [current.visual_style]) if current else [],
            format_func=VISUAL_STYLE_LABELS.get,
            help="Selected styles rotate in order across scheduled slots and persist across restarts.",
        )
        ready_buffer_target = st.number_input(
            "Validated video buffer", 1, 24,
            value=current.ready_buffer_target if current else 6,
        )
        generation_lead_hours = st.number_input(
            "Generate this many hours ahead", 1, 336,
            value=current.generation_lead_hours if current else 48,
        )
        provider_retry_limit = st.number_input(
            "Transient generation retry limit", 0, 5,
            value=current.provider_retry_limit if current else 2,
        )
        weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        schedule_days = st.multiselect("Schedule weekdays", weekday_names, default=[weekday_names[i] for i in (current.schedule_weekdays if current else range(7))])
        profile_cross_dedupe = st.checkbox("Check recent content from other channels by default", value=current.check_cross_channel_duplicates if current else False)
        backend_values = ["youtube_direct", "upload_post"]
        publisher_backend = st.selectbox("Publishing provider", backend_values,
            index=backend_values.index(current.publishing_backend) if current else 0,
            help="Direct YouTube is channel-specific. Upload-Post remains available for cross-platform workflows and legacy jobs.")
        policy_values = ["manual", "private", "unlisted", "scheduled_public", "automatic_public"]
        policy = st.selectbox("Publishing policy", policy_values, index=policy_values.index(current.publishing_policy) if current else 0)
        auto_public_enabled = st.checkbox("Opt in to automatic public publishing", value=current.auto_public_enabled if current else False,
            help="Public release still requires technical QA, originality, rights and repetition checks.")
        allow_unverified_publication = st.checkbox(
            "Allow unattended public uploads when facts are unverified",
            value=current.allow_unverified_publication if current else False,
            help="Unverified public uploads are labeled [UNVERIFIED FACTS] in the title and described as unverified.",
        )
        made_for_kids = st.checkbox("This channel/video is made for kids", value=current.youtube_made_for_kids if current else False)
        synthetic_media = st.checkbox("Contains realistic altered or synthetic media requiring disclosure", value=current.contains_synthetic_media if current else False)
        enabled = st.checkbox("Profile enabled", value=current.enabled if current else True)
        account_profiles = []
        account_error = None
        account = None
        if publisher_backend == "upload_post":
            try:
                account_profiles = channel_profiles.upload_post_profiles()
            except channel_profiles.UploadProfileError as exc:
                account_error = str(exc)
            if account_error:
                st.warning(f"Upload-Post status unavailable: {account_error}")
            account_options = [item["username"] for item in account_profiles if item.get("username")]
            old_account = current.upload_post_username if current else None
            account_index = account_options.index(old_account) if old_account in account_options else None
            account = st.selectbox(
                "Upload-Post YouTube profile",
                account_options,
                index=account_index,
                placeholder="Select a connected YouTube profile (optional)",
                help="Only connected profiles can be bound. Provider credentials are not stored here.",
            )
            if account:
                status = next((item for item in account_profiles if item.get("username") == account), {})
                st.caption("YouTube connected" if status.get("youtube_connected") and not status.get("reauth_required") else "YouTube disconnected or reauthorization required")
        else:
            st.caption("Upload-Post profile discovery is not called for a direct YouTube profile.")
        submitted = st.form_submit_button("Save profile", type="primary")

    if submitted:
        try:
            from app.models.channel_profile import ChannelProfile
            style_defaults = visual_style_service.preset_for(
                visual_style_value
            ).defaults
            if current and visual_style_value != current.visual_style:
                if narrator_preset == current.narrator_preset:
                    narrator_preset = style_defaults["narrator_preset"]
                if profanity_level == current.profanity_level:
                    profanity_level = style_defaults["profanity_level"]
                if subtitle_mode == current.subtitle_display_mode:
                    subtitle_mode = style_defaults["caption_preset"]
                if subtitle_animation == current.subtitle_animation:
                    subtitle_animation = style_defaults["subtitle_animation"]
                if int(duration) == current.target_duration_seconds:
                    duration = 30 if visual_style_value == "savage_animated_explainer" else current.target_duration_seconds
                if voice_name == current.voice_name:
                    if (visual_style_value == "savage_animated_explainer"
                            and config.app.get("gemini_api_key")):
                        voice_name = style_defaults["voice_name"]
                    elif current.visual_style == "savage_animated_explainer":
                        voice_name = config.ui.get("voice_name", "")
            values = dict(
                display_name=display_name, channel_hashtag=channel_hashtag.strip(), niche=niche,
                topic_preferences=[line.strip() for line in topics.splitlines() if line.strip()],
                language=language, script_style=script_style,
                target_duration_seconds=int(duration), video_aspect=aspect,
                duration_enforcement=duration_enforcement,
                fact_verification_policy=fact_verification_policy,
                visual_style=visual_style_value,
                narrator_preset=narrator_preset,
                profanity_level=profanity_level,
                stock_video_style=("satisfying_asmr_cutting" if visual_style_value == "asmr_cutting" else "default"),
                asmr_categories=categories or ["kinetic_sand", "soap"],
                video_source=source, voice_name=voice_name, voice_rate=voice_rate, voice_volume=voice_volume, subtitle_enabled=subtitle,
                subtitle_position=subtitle_position, subtitle_display_mode=subtitle_mode,
                hindi_alignment_policy=hindi_alignment_policy,
                subtitle_animation=subtitle_animation, font_name=font_name,
                bgm_type=bgm_type, bgm_file=bgm_file, bgm_volume=bgm_volume,
                generation_settings=json.loads(provider_settings_text or "{}"),
                generation_provider=provider, timezone=timezone_name,
                publications_per_week=int(frequency), publishing_policy=policy,
                visual_style_rotation=rotation_styles,
                ready_buffer_target=int(ready_buffer_target),
                generation_lead_hours=int(generation_lead_hours),
                provider_retry_limit=int(provider_retry_limit),
                publishing_backend=publisher_backend, auto_public_enabled=auto_public_enabled,
                allow_unverified_publication=allow_unverified_publication,
                youtube_made_for_kids=made_for_kids, contains_synthetic_media=synthetic_media,
                youtube_connection_id=(current.youtube_connection_id if current and publisher_backend == "youtube_direct" else None),
                youtube_channel_id=(current.youtube_channel_id if current and publisher_backend == "youtube_direct" else None),
                schedule_times_local=[item.strip() for item in schedule_times.split(",") if item.strip()],
                schedule_weekdays=[weekday_names.index(item) for item in schedule_days],
                check_cross_channel_duplicates=profile_cross_dedupe,
                upload_post_username=account or None, enabled=enabled,
                intro_enabled=current.intro_enabled if current else False,
                intro_text=current.intro_text if current else "",
                intro_audio=current.intro_audio if current else None,
                outro_enabled=current.outro_enabled if current else False,
                outro_text=current.outro_text if current else "",
                outro_audio=current.outro_audio if current else None,
            )
            # A form update must retain the channel's local ID: the direct YouTube
            # connection is cryptographically and logically pinned to that identity.
            if current:
                values["channel_id"] = current.channel_id
            draft = ChannelProfile(**values)
            channel_profiles.verify_publisher_binding(draft)
            if current:
                saved = channel_profiles.store.update(current.channel_id, draft, current.version)
            else:
                saved = channel_profiles.store.create(draft)
            st.session_state["autopilot_select_after_save"] = saved.channel_id
            st.success(f"Saved {saved.display_name} as version {saved.version}.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

    if current:
        if current.enabled and st.button("Disable profile", key="autopilot_disable_profile"):
            disabled = current.model_copy(update={"enabled": False})
            channel_profiles.store.update(current.channel_id, disabled, current.version)
            st.rerun()
        elif not current.enabled and st.button("Enable profile", key="autopilot_enable_profile"):
            enabled_profile = current.model_copy(update={"enabled": True})
            channel_profiles.store.update(current.channel_id, enabled_profile, current.version)
            st.rerun()
        st.subheader("Resolved generation parameters")
        st.json(channel_profiles.build_preview(current).model_dump(mode="json"))
        st.caption(f"Profile ID: {current.channel_id} · Version {current.version} · Timezone {current.timezone}")
        autopilot = autopilot_service.get_service()
        if current.publishing_backend == "youtube_direct":
            connection = autopilot.youtube_connections.connection_for_channel(current.channel_id)
            st.write("YouTube direct connection", connection or {"state": "not_connected"})
            if st.button("Authorize / reconnect this YouTube channel", key=f"youtube_oauth_{current.channel_id}"):
                try:
                    _, authorization_url = autopilot.youtube_connections.begin_oauth(current.channel_id)
                    st.markdown(f"[Continue Google authorization]({authorization_url})")
                except Exception as exc:
                    st.error(str(exc))
            st.caption("The callback verifies the authorized channel ID and binds it to this profile. Each local channel profile needs its own authorization.")
        st.subheader("Monetization readiness")
        st.json(autopilot.readiness_report(current.channel_id))
        schedule_state = autopilot.schedule_state(current.channel_id)
        pause_label = "Resume channel" if schedule_state["paused"] else "Pause channel"
        if st.button(pause_label, key=f"autopilot_pause_{current.channel_id}"):
            autopilot.set_channel_paused(current.channel_id, not schedule_state["paused"], "Changed from Channel Autopilot workspace")
            st.rerun()
        st.caption(f"Schedule {'paused' if schedule_state['paused'] else 'active' if current.enabled and current.publications_per_week else 'inactive'} · {current.publications_per_week}/week · {current.timezone} · {', '.join(current.schedule_times_local)}")
        with st.expander("Publication calendar"):
            st.json(autopilot.store.list_slots(current.channel_id))
        with st.expander("YouTube publication receipts"):
            receipts = autopilot.store.list_publications(current.channel_id)
            if receipts:
                for receipt in receipts:
                    st.write(f"{receipt['state']} · request {receipt['provider_request_id']} · {receipt.get('provider_url') or ''}")
                    if receipt["state"] in {"unknown", "failed"} and st.button("Reconcile / retry confirmed failure", key=f"autopilot_pub_retry_{receipt['job_id']}"):
                        try:
                            autopilot.publish_job(receipt["job_id"])
                            st.rerun()
                        except Exception as exc:
                            st.warning(str(exc))
                    if receipt.get("failure_details"):
                        st.error(receipt["failure_details"])
                    st.json(json.loads(receipt["receipt_json"]) if receipt.get("receipt_json") else receipt)
            else:
                st.caption("No publication requests for this channel.")

        st.divider()
        st.subheader("Content planning")
        st.caption("AI drafted scripts use the configured MPT text provider.")
        with st.form("autopilot_content_plan_form"):
            plan_topic = st.text_input("Topic", key=f"autopilot_topic_{current.channel_id}", placeholder="A video topic or question")
            plan_script = st.text_area("Script preview or draft input", key=f"autopilot_script_{current.channel_id}", height=180, help="Leave blank to draft a narration script with the configured MPT text provider.")
            duration_columns = st.columns(2)
            min_duration = duration_columns[0].number_input("Minimum output seconds", min_value=1.0, max_value=3600.0, value=float(max(1, current.target_duration_seconds - 5)), key=f"autopilot_min_duration_{current.channel_id}")
            max_duration = duration_columns[1].number_input("Maximum output seconds", min_value=1.0, max_value=3600.0, value=float(current.target_duration_seconds), key=f"autopilot_max_duration_{current.channel_id}")
            plan_duration_mode = st.selectbox(
                "Duration enforcement", ["flexible", "warn_only", "strict"],
                index=["flexible", "warn_only", "strict"].index(current.duration_enforcement),
                key=f"autopilot_duration_mode_{current.channel_id}",
            )
            visual_override_label = st.selectbox(
                "Visual Style override",
                ["Use channel default", *VISUAL_STYLE_VALUES],
                key=f"autopilot_visual_override_{current.channel_id}",
            )
            cross_channel_check = st.checkbox("Also check recent content from other channels", value=current.check_cross_channel_duplicates, key=f"autopilot_cross_dedupe_{current.channel_id}")
            create_plan_clicked = st.form_submit_button("Create content plan", type="primary")
        if create_plan_clicked:
            try:
                from app.models.autopilot import ContentPlanCreate
                payload = ContentPlanCreate(
                    topic=plan_topic,
                    script=plan_script,
                    check_cross_channel_duplicates=cross_channel_check,
                    min_duration_seconds=min_duration,
                    max_duration_seconds=max_duration,
                    duration_enforcement=plan_duration_mode,
                    visual_style_override={
                        "Use channel default": None,
                        **VISUAL_STYLE_VALUES,
                    }[visual_override_label],
                )
                plan = autopilot_service.get_service().plan(current.channel_id, payload)
                st.session_state[f"autopilot_selected_plan_{current.channel_id}"] = plan.plan_id
                st.success("Content plan saved. Review the script and duration estimate below.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

        plans = autopilot_service.get_service().store.list_plans(current.channel_id)
        if plans:
            plan_labels = [f"{plan.topic} · {plan.state} · {plan.plan_id[:8]}" for plan in plans]
            selected_plan_id = st.session_state.get(f"autopilot_selected_plan_{current.channel_id}")
            selected_plan_index = next((i for i, plan in enumerate(plans) if plan.plan_id == selected_plan_id), 0)
            selected_plan_label = st.selectbox("Planned content", plan_labels, index=selected_plan_index, key=f"autopilot_plan_select_{current.channel_id}")
            selected_plan = plans[plan_labels.index(selected_plan_label)]
            st.session_state[f"autopilot_selected_plan_{current.channel_id}"] = selected_plan.plan_id
            st.markdown("**Script preview**")
            st.text_area("Narration script", value=selected_plan.script, height=180, disabled=True, key=f"autopilot_plan_script_view_{selected_plan.plan_id}")
            st.caption(
                f"Visual style: requested {selected_plan.visual_style_override or 'channel default'} · "
                f"selected {selected_plan.effective_visual_style or 'pending footage preflight'} · "
                f"{selected_plan.visual_style_reason}"
            )
            st.caption(f"Estimated narration: {selected_plan.duration_estimate_seconds:.1f}s · Requested output: {selected_plan.min_duration_seconds:.1f}–{selected_plan.max_duration_seconds:.1f}s")
            if selected_plan.state == "planned":
                review_columns = st.columns(2)
                accept_estimate = review_columns[0].checkbox("Accept the narration duration estimate if outside the requested range", key=f"autopilot_duration_override_{selected_plan.plan_id}")
                if st.button("Mark plan ready", key=f"autopilot_ready_{selected_plan.plan_id}"):
                    try:
                        autopilot_service.get_service().ready(current.channel_id, selected_plan.plan_id, accept_estimate)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
            elif selected_plan.state == "ready":
                if st.button("Create production job", key=f"autopilot_make_job_{selected_plan.plan_id}", type="primary"):
                    try:
                        autopilot_service.get_service().create_job(current.channel_id, selected_plan.plan_id)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

        st.divider()
        st.subheader("Production jobs")
        jobs = autopilot_service.get_service().store.list_jobs(current.channel_id)
        if not jobs:
            st.info("No production jobs for this channel yet.")
        for job in jobs:
            with st.expander(f"{job.topic} · {job.state} · {job.job_id[:8]}", expanded=job.state in {"failed", "generated"}):
                st.caption(f"Profile v{job.profile_version} · Attempt {job.attempt_count} · MPT task {job.mpt_task_id or 'not started'}")
                st.caption(
                    f"Estimated narration: {job.duration_estimate_seconds:.1f}s · "
                    f"requested {job.min_duration_seconds:.1f}–{job.max_duration_seconds:.1f}s · "
                    f"mode {job.duration_enforcement}"
                )
                if job.actual_duration_seconds is not None:
                    st.caption(
                        f"Actual video: {job.actual_duration_seconds:.2f}s · "
                        f"outside requested range: {job.duration_outside_requested_range}"
                    )
                if job.failure_details:
                    st.error(job.failure_details)
                try:
                    qa = autopilot.qa_report(job.job_id) if job.state == "generated" else None
                    if qa:
                        st.write("QA checks")
                        st.json(qa)
                        content_review = autopilot.store.content_review(job.job_id)
                        with st.form(f"autopilot_content_quality_{job.job_id}"):
                            originality = st.selectbox("Originality review", ["needs_review", "original", "needs_revision"],
                                index=(["needs_review", "original", "needs_revision"].index(content_review["originality_status"])
                                       if content_review["originality_status"] in {"needs_review", "original", "needs_revision"} else 0))
                            rights = st.selectbox("Footage rights review", ["missing_evidence", "documented", "needs_review"],
                                index=(["missing_evidence", "documented", "needs_review"].index(content_review["rights_status"])
                                       if content_review["rights_status"] in {"missing_evidence", "documented", "needs_review"} else 2))
                            quality_notes = st.text_area("Originality and rights review notes", value=content_review.get("notes", ""))
                            asset_materials = autopilot._material_sources_for_job(job)
                            rights_evidence = []
                            old_evidence = {item.get("asset_key"): item.get("evidence", "")
                                for item in content_review.get("rights_evidence", []) if isinstance(item, dict)}
                            for asset_index, asset in enumerate(asset_materials):
                                asset_key = str(asset.get("asset_id") or asset.get("local_file") or asset.get("source_page") or asset_index)
                                evidence = st.text_input(f"Rights evidence for asset {asset_key}", value=old_evidence.get(asset_key, ""),
                                    key=f"autopilot_rights_{job.job_id}_{asset_index}")
                                if evidence.strip():
                                    rights_evidence.append({"asset_key": asset_key, "evidence": evidence.strip()})
                            save_quality = st.form_submit_button("Save originality and rights review")
                        if save_quality:
                            try:
                                from app.models.autopilot import ContentQualityReview
                                autopilot.review_content_quality(job.job_id, "operator", ContentQualityReview(
                                    originality_status=originality, rights_status=rights, notes=quality_notes,
                                    rights_evidence=rights_evidence))
                                st.rerun()
                            except Exception as exc:
                                st.error(str(exc))
                        if qa["passed"]:
                            review_cols = st.columns(2)
                            if review_cols[0].button("Approve", key=f"autopilot_approve_{job.job_id}"):
                                autopilot.review_job(job.job_id, "approve")
                                st.rerun()
                            if review_cols[1].button("Reject", key=f"autopilot_reject_{job.job_id}"):
                                autopilot.review_job(job.job_id, "reject")
                                st.rerun()
                            if job.profile_snapshot.get("publishing_policy") in {"manual", "private", "unlisted", "scheduled_public"} and st.button("Publish using pinned account", key=f"autopilot_publish_{job.job_id}"):
                                autopilot.publish_job(job.job_id)
                                st.rerun()
                except Exception as exc:
                    st.warning(f"QA/publication status unavailable: {exc}")
                if job.actual_duration_seconds is not None:
                    st.caption(f"Rendered duration: {job.actual_duration_seconds:.2f}s (requested {job.min_duration_seconds:.1f}–{job.max_duration_seconds:.1f}s)")
                action_columns = st.columns(3)
                if job.state == "ready" and action_columns[0].button("Start generation", key=f"autopilot_start_{job.job_id}"):
                    try:
                        autopilot_service.get_service().start_job(job.job_id)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
                elif job.state == "queued" and action_columns[0].button("Resume queued job", key=f"autopilot_resume_{job.job_id}"):
                    try:
                        autopilot_service.get_service().start_job(job.job_id)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
                if job.state == "failed" and action_columns[1].button("Retry pinned job", key=f"autopilot_retry_{job.job_id}"):
                    try:
                        autopilot_service.get_service().retry_job(job.job_id)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
                if job.state == "queued" and action_columns[2].button("Cancel queued job", key=f"autopilot_cancel_{job.job_id}"):
                    try:
                        autopilot_service.get_service().cancel_job(job.job_id)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
                if job.state == "generating":
                    st.info("Generation is running. The current MPT pipeline does not support cooperative cancellation.")
                    if st.button("Check for interrupted worker", key=f"autopilot_recover_{job.job_id}"):
                        try:
                            autopilot_service.get_service().store.recover_job(job.job_id)
                            st.rerun()
                        except Exception as exc:
                            st.warning(str(exc))
                for artifact in job.output_artifacts:
                    if os.path.isfile(artifact):
                        st.video(artifact)
                        st.caption(f"Output artifact: {artifact}")
                    else:
                        st.code(artifact)


autopilot_service.get_service()  # Start the persistent schedule poller in the WebUI process.
_render_application()
