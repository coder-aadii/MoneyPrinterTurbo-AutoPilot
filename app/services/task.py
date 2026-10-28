import json
import hashlib
import math
import os
import re
import socket
import tempfile
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from functools import partial
from os import path
from pathlib import Path
from uuid import uuid4

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams
from app.services import bgm as bgm_service
from app.services import (
    asmr,
    animated_explainer,
    elevenlabs_music,
    llm,
    script_quality,
    loomloom,
    material,
    metaso_minimax,
    muapi,
    ofox,
    sonilo,
    subtitle,
    task_artifacts,
    twelvelabs,
    source_research,
    video,
    volcengine_seedance,
    voice,
)
from app.services import visual_style
from app.services import emotion_storyboard
from app.services import upload_post
from app.services import generation_settings
from app.services import hindi_script
from app.services import state as sm
from app.utils import file_security, utils
from app.utils.subtitle_writer import staged_subtitle_file


# 发布请求最长可等待数分钟，不能继续占用视频生成任务的并发名额。
# 固定大小的线程池将发布吞吐限制在可控范围内，同时让视频产物生成后
# 立即进入完成状态。
_cross_post_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="mpt-cross-post",
)
_cross_post_max_pending_tasks = max(
    1,
    int(config.app.get("upload_post_max_pending_tasks", 10)),
)
_cross_post_slots = threading.BoundedSemaphore(_cross_post_max_pending_tasks)
_cross_post_registry_lock = threading.RLock()
_cross_post_futures: dict[str, Future] = {}
_cross_post_process_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
_ACTIVE_CROSS_POST_STATES = {
    const.CROSS_POST_STATE_PENDING,
    const.CROSS_POST_STATE_PROCESSING,
}
_CROSS_POST_STATE_WRITE_ATTEMPTS = 3
_CROSS_POST_STATE_RETRY_DELAY_SECONDS = 0.1
_TASK_FAILURE_STATE_WRITE_ATTEMPTS = 3
_TASK_FAILURE_STATE_RETRY_DELAY_SECONDS = 0.1
_LOOMLOOM_STATE_WRITE_ATTEMPTS = 3
_LOOMLOOM_STATE_RETRY_DELAY_SECONDS = 0.1
_INTERRUPTED_CROSS_POST_ERROR = (
    "cross-posting was interrupted before the process completed"
)


def _represent_script(params, script):
    return hindi_script.build_representations(
        script,
        getattr(params, "video_language", ""),
        pronunciation_overrides=getattr(params, "pronunciation_overrides", None),
    )


def _requires_hindi_word_alignment(params) -> bool:
    language = str(getattr(params, "video_language", "") or "").casefold()
    if (not language.startswith(("hi", "hindi"))
            or not getattr(params, "subtitle_enabled", True)
            or getattr(params, "subtitle_display_mode", "sentence") != "word_by_word"):
        return False
    provider = generation_settings.resolve_task_settings(
        params, topic=getattr(params, "video_subject", "")
    ).tts_provider
    return (
        provider == "gemini-tts"
        or voice.is_gemini_voice(getattr(params, "voice_name", "") or "")
    )


def _alignment_preflight_errors(params, spoken_script, source_text=None):
    if not _requires_hindi_word_alignment(params):
        return []
    return subtitle.preflight_hindi_alignment_script(spoken_script, source_text=source_text)


# Map upload-post platform ids to the social platform names llm.py accepts.
_CROSS_POST_SOCIAL_PLATFORMS = {
    "tiktok": "tiktok",
    "instagram": "instagram_reels",
    "facebook": "facebook_reels",
}
# 视频配乐服务只需实现 ``is_enabled`` 和 ``generate_bgm``。供应商差异集中在
# 文件扩展名、领域异常和 WebUI 警告代码；任务编排、0 音量短路及失败降级
# 全部复用同一路径，避免后续新增供应商时维护多份相似流程。
_VIDEO_MUSIC_PROVIDERS = {
    "sonilo": {
        "service": sonilo,
        "error_type": sonilo.SoniloError,
        "suffix": ".m4a",
        "warning_code": "sonilo_bgm_failed",
        "display_name": "Sonilo",
    },
    "elevenlabs": {
        "service": elevenlabs_music,
        "error_type": elevenlabs_music.ElevenLabsMusicError,
        "suffix": ".mp3",
        "warning_code": "elevenlabs_bgm_failed",
        "display_name": "ElevenLabs",
    },
}


_SUPPORTED_VIDEO_SOURCES = frozenset({
    "auto", "pexels", "pixabay", "coverr", "local", "wavespeed",
    "volcengine_seedance", "ofox", "metaso_minimax", "muapi",
    "loomloom", "openai_image",
})


def _get_video_music_prompt(params: VideoParams) -> str:
    """
    读取当前视频配乐供应商实际使用的提示词。

    新任务统一使用供应商无关字段；旧 Sonilo CLI 参数和历史任务仍可能只有
    ``sonilo_bgm_prompt``，因此仅在 Sonilo 通用字段为空时读取旧字段。
    """
    prompt = str(params.video_music_prompt or "").strip()
    if params.bgm_type == "sonilo" and not prompt:
        prompt = str(params.sonilo_bgm_prompt or "").strip()
    return prompt


def is_task_busy(task: dict | None) -> bool:
    """判断任务是否仍在生成或发布，供所有删除入口复用。"""
    if not task:
        return False

    state = task.get("state")
    try:
        state = int(state)
    except (TypeError, ValueError):
        # The function's own docstring promises to treat unparseable
        # states as busy, so the delete API keeps a task that we
        # cannot reason about. Returning False here would let
        # delete_video() cancel a task that is still running or
        # cross-posting whenever an older Redis snapshot or a
        # legacy call site stored the state as a string.
        return True

    # 视频生成和跨平台发布都可能继续读取任务目录。统一视为忙碌状态，
    # 可以避免 API 与 WebUI 分别维护规则后出现一个允许删除、另一个禁止
    # 删除的不一致行为。
    return (
        state == const.TASK_STATE_PROCESSING
        or task.get("cross_post_state") in _ACTIVE_CROSS_POST_STATES
    )


def _register_cross_post_future(task_id: str, future: Future) -> None:
    """登记当前进程持有的发布 Future，供启动恢复和测试判断真实运行状态。"""
    with _cross_post_registry_lock:
        _cross_post_futures[task_id] = future


def _unregister_cross_post_future(task_id: str, future: Future | None = None) -> None:
    """仅移除匹配的 Future，避免旧回调误删同任务后续注册的新工作。"""
    with _cross_post_registry_lock:
        current = _cross_post_futures.get(task_id)
        if current is None or (future is not None and current is not future):
            return
        _cross_post_futures.pop(task_id, None)


def _is_cross_post_active_in_process(task_id: str) -> bool:
    """判断当前进程是否仍持有未结束的发布任务。"""
    with _cross_post_registry_lock:
        future = _cross_post_futures.get(task_id)
        return future is not None and not future.done()


def _is_windows_process_alive(process_id: int) -> bool:
    """通过只读 Win32 API 判断进程状态，避免用 os.kill 误终止进程。"""
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # ctypes 默认把未声明的返回值当作 32 位 int。Windows 64 位进程句柄可能
    # 因此被截断，必须显式声明 Win32 函数签名后再调用。
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        error_code = ctypes.get_last_error()
        if error_code == error_invalid_parameter:
            return False
        if error_code == error_access_denied:
            # 进程存在但当前用户无查询权限时，必须保守地视为存活，避免错误
            # 回收其它账户正在执行的发布任务。
            return True
        logger.warning(
            "failed to open cross-post owner process on Windows, "
            f"process_id: {process_id}, error_code: {error_code}"
        )
        return True

    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            error_code = ctypes.get_last_error()
            logger.warning(
                "failed to read cross-post owner process state on Windows, "
                f"process_id: {process_id}, error_code: {error_code}"
            )
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _is_cross_post_owner_alive(owner: str | None) -> bool:
    """判断持久化发布任务的本机进程是否仍存在。"""
    if not owner:
        return False

    try:
        hostname, process_id_text, _ = owner.split(":", 2)
        process_id = int(process_id_text)
    except (TypeError, ValueError):
        logger.warning(f"invalid cross-post owner metadata: {owner}")
        return False

    # 无法可靠探测其它主机上的进程。共享 Redis 的多主机部署中必须保守地
    # 视为仍在运行，避免当前节点误删另一节点正在读取的视频文件。
    if hostname != socket.gethostname():
        return True

    # 当前进程内是否仍有真实发布工作，已经由 Future 注册表准确判断。运行到
    # 这里说明注册表中没有对应 Future，即使 owner 与当前进程完全一致，也应
    # 视为已中断；这可以覆盖终态写入持续失败、Future 已结束的场景。
    if process_id == os.getpid():
        return False

    # Windows 的 os.kill(pid, 0) 与 POSIX 语义不同，可能直接终止目标进程。
    # 使用只申请查询权限的 Win32 API，不向目标进程发送任何信号。
    if os.name == "nt":
        return _is_windows_process_alive(process_id)

    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        logger.warning(
            f"failed to inspect cross-post owner process, owner: {owner}, error: {exc}"
        )
        return True
    return True


def _mark_task_failed(
    task_id: str,
    stage: str,
    error: str,
    details: dict | None = None,
) -> dict:
    """记录结构化失败信息，并保留任务失败前已经到达的进度。"""
    existing_task = None
    try:
        existing_task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.warning(f"failed to read task state before failure update: {exc}")

    # 具体服务函数通常比编排层拥有更准确的错误原因。后续的空结果检查
    # 不能再用通用文案覆盖它，否则 API 调用方仍然只能看到模糊信息。
    if (
        existing_task
        and existing_task.get("state") == const.TASK_STATE_FAILED
        and existing_task.get("error")
    ):
        return existing_task

    message = str(error or "unknown task error").strip()
    progress = int((existing_task or {}).get("progress", 0) or 0)
    logger.error(f"task failed, task_id: {task_id}, stage: {stage}, error: {message}")
    failure = {
        "task_id": task_id,
        "state": const.TASK_STATE_FAILED,
        "progress": progress,
        "failed_stage": stage,
        "error": message,
    }
    # 某些外部任务已经创建了可用于恢复或排障的远端 ID。失败状态需要保留
    # 这些非敏感字段，但不能允许调用方覆盖统一的状态、进度和错误结构。
    # Keep completed batch outputs in both the persisted state and the direct
    # return value used by the CLI. These fields are reset before a new attempt.
    failure_details = {
        key: existing_task[key]
        for key in ("videos", "combined_videos", "warnings", "failed_video_index")
        if existing_task and key in existing_task
    }
    failure_details.update({
        key: value for key, value in dict(details or {}).items() if key not in failure
    })
    failure.update(failure_details)
    fields = {key: value for key, value in failure.items() if key != "task_id"}
    for attempt in range(_TASK_FAILURE_STATE_WRITE_ATTEMPTS):
        try:
            if attempt == 0:
                sm.state.update_task(task_id, **fields)
            else:
                # A lost acknowledgement may mean the first write succeeded.
                # Patch only an existing record so retries cannot revive a task
                # that the user has since deleted.
                sm.state.patch_task(task_id, **fields)
            break
        except Exception as exc:
            if attempt + 1 == _TASK_FAILURE_STATE_WRITE_ATTEMPTS:
                logger.exception(
                    f"failed to persist task failure, task_id: {task_id}, error: {exc}"
                )
            else:
                time.sleep(_TASK_FAILURE_STATE_RETRY_DELAY_SECONDS)
    # A state outage must not discard the only completed-file snapshot held by
    # the caller. Return it even when all bounded persistence attempts fail.
    return failure


def _duration_outside_range(actual: float | None, minimum: float | None,
                            maximum: float | None) -> bool | None:
    if actual is None:
        return None
    return bool((minimum is not None and actual < minimum)
                or (maximum is not None and actual > maximum))


def _duration_metadata(params, *, narration: float | None = None,
                       final_video: float | None = None) -> dict:
    measured = final_video if final_video is not None else narration
    return {
        "requested_min_duration_seconds": params.min_duration_seconds,
        "requested_max_duration_seconds": params.max_duration_seconds,
        "actual_narration_duration_seconds": narration,
        "actual_final_video_duration_seconds": final_video,
        "duration_enforcement": params.duration_enforcement,
        "duration_outside_requested_range": _duration_outside_range(
            measured, params.min_duration_seconds, params.max_duration_seconds
        ),
        "narration_outside_requested_range": _duration_outside_range(
            narration, params.min_duration_seconds, params.max_duration_seconds
        ),
        "final_video_outside_requested_range": _duration_outside_range(
            final_video, params.min_duration_seconds, params.max_duration_seconds
        ),
    }


def _write_merged_subtitles(cue_blocks: list[str], output_path: str) -> bool:
    """Write already-offset subtitle blocks, independent of narration caption state."""
    import re

    blocks = []
    for text in cue_blocks:
        blocks.extend(block.strip() for block in re.split(r"\n\s*\n", text) if block.strip())
    if not blocks:
        return False
    with open(output_path, "w", encoding="utf-8") as output:
        for index, block in enumerate(blocks, start=1):
            lines = block.splitlines()
            lines[0] = str(index)
            output.write("\n".join(lines) + "\n\n")
    return True


def _measure_video_duration(video_path: str) -> float | None:
    try:
        clip = video._open_video_clip_quietly(video_path, audio=False)
        try:
            duration = float(clip.duration or 0)
            return duration if math.isfinite(duration) and duration > 0 else None
        finally:
            clip.close()
    except Exception as exc:
        logger.warning("could not measure final video duration: file={}, error={}",
                       path.basename(video_path), type(exc).__name__)
        return None


def _stage_progress_reporter(task_id: str, stage_start: float, stage_end: float):
    """
    返回一个回调，把阶段内 0~1 的完成比例换算成任务进度并写入状态。

    流水线原先只在阶段边界更新进度，下载素材和处理片段这类耗时阶段期间
    进度条完全不动，用户无法区分仍在运行与已经卡死。进度只是展示信息：
    比例无法解析时忽略，状态后端写入失败时只记录日志，不能因此中断下载或
    合成。
    """

    def report(fraction) -> None:
        try:
            fraction = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            return
        try:
            sm.state.update_task(
                task_id,
                state=const.TASK_STATE_PROCESSING,
                progress=stage_start + (stage_end - stage_start) * fraction,
            )
        except Exception as exc:
            logger.warning(
                f"failed to update task progress: task_id={task_id}, "
                f"error={type(exc).__name__}, detail={exc}"
            )

    return report


def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    settings = generation_settings.resolve_task_settings(
        params, topic=params.video_subject
    )
    video_script = params.video_script.strip()
    if not video_script:
        script_prompt = params.video_script_prompt
        if params.min_duration_seconds is not None or params.max_duration_seconds is not None:
            minimum = params.min_duration_seconds
            maximum = params.max_duration_seconds
            if minimum is not None and maximum is not None:
                target = (minimum + maximum) / 2
                range_hint = f"requested range {minimum:g}–{maximum:g} seconds"
            else:
                target = minimum if minimum is not None else maximum
                range_hint = f"requested target {target:g} seconds"
            enforcement_hint = (
                "Stay within the requested range because Strict enforcement is enabled."
                if params.duration_enforcement == "strict"
                else "Treat duration as a guide; prioritize a complete, natural narration."
            )
            script_prompt = "\n".join(filter(None, (
                script_prompt,
                f"Narration duration target: about {target:g} seconds ({range_hint}). "
                f"{enforcement_hint} Do not pad or omit meaningful explanation.",
            )))
        cache_payload = {
            "topic": params.video_subject, "language": params.video_language,
            "paragraph_number": params.paragraph_number, "script_prompt": script_prompt,
            "custom_system_prompt": params.custom_system_prompt,
            "visual_style": settings.effective_visual_style,
            "narrator_preset": settings.narrator_preset,
            "profanity_level": settings.profanity_level,
            "savage_quality_policy": params.savage_quality_policy,
            "llm_provider": config.app.get("llm_provider"),
        }
        provider = llm.get_llm_provider(cache_payload["llm_provider"])
        if provider is not None:
            cache_payload["llm_model"] = config.app.get(provider.config_key("model_name"))
            cache_payload["llm_base_url"] = config.app.get(provider.config_key("base_url"))
        cache_fingerprint = task_artifacts.cache_key(cache_payload)
        draft_cache = path.join(utils.task_dir(task_id), "script-draft-cache.json")
        try:
            with open(draft_cache, encoding="utf-8") as source:
                cached = json.load(source)
            video_script = cached.get("script", "") if cached.get("fingerprint") == cache_fingerprint else ""
            if video_script:
                logger.info("reusing validated task-local LLM script draft: task_id={}", task_id)
        except (OSError, ValueError, TypeError):
            video_script = ""
        if not video_script:
            video_script = llm.generate_script(
                video_subject=params.video_subject,
                language=params.video_language,
                paragraph_number=params.paragraph_number,
                video_script_prompt=script_prompt,
                custom_system_prompt=params.custom_system_prompt,
                visual_style=settings.effective_visual_style,
                narrator_preset=settings.narrator_preset,
                profanity_level=settings.profanity_level,
                savage_quality_policy=params.savage_quality_policy,
            )
            if video_script and not video_script.startswith("Error: "):
                try:
                    task_artifacts._write_json_atomic(Path(draft_cache), {
                        "fingerprint": cache_fingerprint, "script": video_script,
                    })
                except OSError as exc:
                    logger.warning(
                        "could not cache successful script draft: task_id={}, error={}",
                        task_id, type(exc).__name__,
                    )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        _mark_task_failed(task_id, "script", "failed to generate video script")
        return None

    return video_script


def _verify_script_for_policy(task_id, params: VideoParams, script: str) -> str | None:
    """Verify the exact spoken text (including numeric expansion) before synthesis."""
    settings = generation_settings.resolve_task_settings(
        params, topic=params.video_subject
    )
    params.effective_visual_style = settings.effective_visual_style
    spoken_script = _represent_script(params, script).spoken_script
    verification_hash = hashlib.sha256(spoken_script.encode("utf-8")).hexdigest()
    # Never carry a previous report/evidence set into a new generation attempt,
    # even when the narration text happens to be identical.
    _clear_fact_verification(params)
    policy = getattr(params, "fact_verification_policy", "advisory")
    if policy == "off":
        params.fact_verification_state = "unverified"
        params.fact_verification_report = {
            "state": "unverified", "status": "unverified",
            "message": "Fact checking is off for this job.",
            "script_hash": verification_hash,
        }
        params.fact_verification_script_hash = verification_hash
        sm.state.update_task(
            task_id, fact_verification_state="unverified",
            fact_verification_status="unverified",
            fact_verification_policy=policy,
            fact_verification_script_hash=verification_hash,
            fact_verification_report=params.fact_verification_report,
        )
        return script

    try:
        timeout_seconds = None
        if policy == "advisory":
            timeout_seconds = getattr(params, "fact_research_timeout_seconds", None)
            if timeout_seconds is None:
                timeout_seconds = config.app.get("fact_research_timeout_seconds", 8)
            try:
                timeout_seconds = max(0.1, min(float(timeout_seconds), 60.0))
            except (TypeError, ValueError):
                timeout_seconds = 8.0
            params.fact_research_timeout_seconds = timeout_seconds
        if policy == "advisory":
            report = source_research.verify_script(
                spoken_script, timeout_seconds=timeout_seconds
            )
        else:
            report = source_research.verify_script(spoken_script)
    except source_research.ResearchUnavailable as exc:
        report = {
            "state": "unavailable", "verification_error": str(exc), "claims": [],
            "script_hash": verification_hash,
        }
    except Exception as exc:
        logger.exception(f"fact verification failed, task_id={task_id}, error={exc}")
        report = {
            "state": "verification_error", "verification_error": type(exc).__name__,
            "claims": [], "script_hash": verification_hash,
        }

    if not isinstance(report, dict) or report.get("script_hash") != verification_hash:
        report = {
            "state": "verification_error",
            "verification_error": "verification report does not match the current script hash",
            "script_hash": verification_hash,
            "claims": [],
        }
    params.fact_verification_report = report
    verification_state = report.get("state", "unavailable")
    if verification_state not in {
        "unverified", "source_verified", "unsupported", "contradicted",
        "unavailable", "verification_error",
    }:
        verification_state = "verification_error"
    params.fact_verification_state = verification_state
    params.fact_verification_script_hash = verification_hash
    verification_status = _fact_verification_status(verification_state)
    if params.fact_verification_state == "source_verified":
        claims = report.get("claims", [])
        params.fact_claims = [str(item.get("claim", "")) for item in claims if item.get("claim")][:20]
        params.fact_evidence = [
            {
                "claim_id": str(item.get("claim_id", "")),
                "claim": str(item.get("claim", "")),
                "evidence_id": str(evidence.get("evidence_id", "")),
                "title": str(evidence.get("title", "")),
                "url": str(evidence.get("url", "")),
                "excerpt": str(evidence.get("excerpt", "")),
            }
            for item in claims
            for evidence in item.get("evidence", [])
            if evidence.get("evidence_id") in item.get("verified_evidence_ids", [])
        ][:20]
    else:
        logger.warning(
            f"continuing Savage generation without verified claims, task_id={task_id}, "
            f"state={params.fact_verification_state}"
        )

    try:
        sm.state.update_task(
            task_id,
            fact_verification_state=params.fact_verification_state,
            fact_verification_status=verification_status,
            fact_verification_policy=params.fact_verification_policy,
            fact_verification_script_hash=verification_hash,
            fact_verification_report=report,
        )
    except Exception as exc:
        logger.warning(
            f"could not persist optional fact-verification report, task_id={task_id}, "
            f"error={type(exc).__name__}"
        )
    if (params.fact_verification_policy == "strict"
            and params.fact_verification_state != "source_verified"):
        _mark_task_failed(
            task_id,
            "fact_verification",
            "Strict factual verification did not establish support for the exact narration; TTS was not started.",
            details={
                "fact_verification_state": params.fact_verification_state,
                "fact_verification_script_hash": verification_hash,
                "fact_verification_report": report,
            },
        )
        return None
    return script


def _clear_fact_verification(params: VideoParams) -> None:
    """Invalidate evidence whenever the narration text changes."""
    params.fact_claims = []
    params.fact_evidence = []
    params.fact_verification_report = {}
    params.fact_verification_state = "unverified"
    params.fact_verification_script_hash = ""


def _fact_verification_status(state: str) -> str:
    return {
        "source_verified": "verified",
        "unsupported": "unverified",
        "contradicted": "contradicted",
        "unavailable": "unavailable",
        "verification_error": "unavailable",
        "unverified": "unverified",
    }.get(state, "unavailable")


def _revise_script_for_duration(script: str, params: VideoParams, actual_seconds: float) -> str:
    settings = generation_settings.resolve_task_settings(
        params, topic=params.video_subject
    )
    minimum = params.min_duration_seconds or 0
    maximum = params.max_duration_seconds or float("inf")
    target = (minimum + maximum) / 2 if maximum != float("inf") else minimum
    if target <= 0:
        return ""
    prompt = (
        f"Revise the narration to speak naturally in about {target:.0f} seconds. "
        f"The previous narration measured {actual_seconds:.1f} seconds and must fit "
        f"{minimum:.1f}–{maximum:.1f} seconds. Preserve the topic and language. "
        "Preserve the same story and intended meaning as this current narration:\n"
        f"{script}\nDo not add padding, repetition, unsupported claims, or stage directions; "
        "return only the revised narration."
    )
    if params.video_script_prompt.strip():
        prompt = f"Existing narration requirements: {params.video_script_prompt}\n{prompt}"
    try:
        revised = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number,
            video_script_prompt=prompt,
            custom_system_prompt=params.custom_system_prompt,
            visual_style=settings.effective_visual_style,
            narrator_preset=settings.narrator_preset,
            profanity_level=settings.profanity_level,
        )
    except Exception as exc:
        logger.warning(f"duration revision failed: {type(exc).__name__}: {exc}")
        return ""
    return revised.strip() if isinstance(revised, str) and not revised.startswith("Error: ") else ""


def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    video_terms = params.video_terms
    effective_style = params.effective_visual_style or params.visual_style
    savage_beat_count = None
    if effective_style == visual_style.SAVAGE_ANIMATED_EXPLAINER:
        spoken_for_stock = _represent_script(params, video_script).spoken_script
        savage_beat_count = len(emotion_storyboard.build(
            utils.remove_pause_tags(spoken_for_stock),
            narrator_preset=params.narrator_preset or "desi_savage_aggressive",
            profanity_level=params.profanity_level or "savage_monetization_aware",
        ))
    if effective_style == visual_style.ASMR_CUTTING or params.stock_video_style == asmr.SATISFYING_ASMR_CUTTING:
        # ASMR searches are an independent visual track. Categories, not the
        # narration subject/script or script-order setting, define its queries.
        video_terms = [
            term
            for _category, terms in asmr.category_queries(params.asmr_categories)
            for term in terms
        ]
    elif not video_terms and effective_style == visual_style.RELEVANT_STOCK:
        video_terms = llm.generate_terms(
            video_subject=params.video_subject,
            video_script=utils.remove_pause_tags(video_script),
            amount=8 if params.match_materials_to_script else 5,
            match_script_order=params.match_materials_to_script,
            stock_video_style="default",
        )
    elif not video_terms:
        # 开启素材按文案顺序匹配后，关键词本身也必须按脚本叙事顺序生成；
        # 否则后续即使顺序下载和顺序拼接，也只能复用一组全局主题词，
        # 无法改善“后面内容的画面提前出现”的问题。
        video_terms = llm.generate_terms(
            video_subject=params.video_subject,
            video_script=utils.remove_pause_tags(video_script),
            amount=(
                max(1, int(savage_beat_count or 0))
                if effective_style == visual_style.SAVAGE_ANIMATED_EXPLAINER
                else 8 if params.match_materials_to_script else 5
            ),
            match_script_order=(
                params.match_materials_to_script
                or effective_style == visual_style.SAVAGE_ANIMATED_EXPLAINER
            ),
            stock_video_style=params.stock_video_style,
        )
    else:
        if isinstance(video_terms, str):
            video_terms = [term.strip() for term in re.split(r"[,，\n]", video_terms)]
        elif isinstance(video_terms, list):
            if not all(isinstance(term, str) for term in video_terms):
                raise ValueError("video_terms must contain only strings.")
            video_terms = [term.strip() for term in video_terms]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        # Delimiter-only input must not reach paid reranking or material search.
        video_terms = [term for term in video_terms if term]

        logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if effective_style == visual_style.SAVAGE_ANIMATED_EXPLAINER:
        expected_beats = int(savage_beat_count or 0)
        if len(video_terms or []) != expected_beats:
            _mark_task_failed(
                task_id,
                "terms",
                "Savage stock keywords must map one-to-one to narration beats: "
                f"received {len(video_terms or [])} keyword(s) for {expected_beats} beat(s). "
                "Repeat a keyword for each beat that stays on the same topic.",
                details={"keyword_count": len(video_terms or []), "beat_count": expected_beats},
            )
            return None

    if not video_terms:
        _mark_task_failed(
            task_id,
            "terms",
            "The text provider did not return usable English video search keywords. "
            "Try generating narration keywords again or enter English stock-search terms in Advanced Settings.",
        )
        return None

    if params.stock_video_style == asmr.SATISFYING_ASMR_CUTTING:
        video_terms = [
            term for term in video_terms if asmr.classify_asmr_term(term) is not None
        ]
        if not video_terms:
            _mark_task_failed(
                task_id,
                "terms",
                "Satisfying ASMR Cutting requires a search term combining a "
                "supported material and action, such as 'kinetic sand cutting'.",
            )
            return None

    # 可选的 TwelveLabs Marengo 语义重排：未启用时返回原顺序，无任何副作用。
    # 顺序匹配模式下关键词顺序本身就是脚本叙事顺序，必须保持原样，故跳过。
    if (
        not params.match_materials_to_script
        and params.stock_video_style != asmr.SATISFYING_ASMR_CUTTING
        and effective_style != visual_style.SAVAGE_ANIMATED_EXPLAINER
    ):
        video_terms = twelvelabs.rerank_terms_by_subject(
            video_subject=params.video_subject,
            search_terms=video_terms,
        )

    return video_terms


def save_script_data(task_id, video_script, video_terms, params):
    if (params.effective_visual_style == visual_style.SAVAGE_ANIMATED_EXPLAINER
            or params.visual_style == visual_style.SAVAGE_ANIMATED_EXPLAINER):
        params.stock_media_policy = animated_explainer.resolve_stock_media_policy(
            params.stock_media_policy
        )
    settings = generation_settings.resolve_task_settings(params)
    representations = _represent_script(params, video_script)
    spoken_script = representations.spoken_script
    params.spoken_script = spoken_script
    emotion_timeline = emotion_storyboard.build(
        spoken_script,
        narrator_preset=settings.narrator_preset,
        profanity_level=settings.profanity_level,
    )
    script_data = {
        "script": video_script,
        "spoken_script": spoken_script,
        "display_script": representations.display_script,
        "display_word_map": representations.display_map(),
        "script_hash": hashlib.sha256(video_script.encode("utf-8")).hexdigest(),
        "spoken_script_hash": hashlib.sha256(spoken_script.encode("utf-8")).hexdigest(),
        "duration_metadata": _duration_metadata(params),
        "search_terms": video_terms,
        "stock_media_policy": params.stock_media_policy,
        "params": params,
        "fact_claims": list(getattr(params, "fact_claims", []) or []),
        "fact_evidence": list(getattr(params, "fact_evidence", []) or []),
        "fact_verification_state": getattr(params, "fact_verification_state", "unverified"),
        "fact_verification_status": _fact_verification_status(
            getattr(params, "fact_verification_state", "unverified")
        ),
        "fact_verification_policy": params.fact_verification_policy,
        "fact_verification_script_hash": params.fact_verification_script_hash,
        "fact_verification_report": getattr(params, "fact_verification_report", {}) or {},
        "emotion_timeline": emotion_timeline,
        "resolved_generation_settings": settings.model_dump(mode="json"),
        "visual_style_selection": {
            "requested_mode": params.visual_style,
            "effective_mode": params.effective_visual_style,
            "reason": params.visual_style_reason,
        },
    }
    task_artifacts.write_script_data(task_id, script_data)


def resolve_custom_audio_file(
    task_id: str,
    custom_audio_file: str | None,
    *,
    allow_server_file_input: bool = False,
) -> str:
    requested_file = (custom_audio_file or "").strip()
    if not requested_file:
        return ""

    task_dir = utils.task_dir(task_id)
    try:
        return file_security.resolve_path_within_directory(
            task_dir,
            requested_file,
        )
    except ValueError as exc:
        task_dir_error = exc

    # A missing path that otherwise stays inside the task directory is safe to
    # report precisely. Paths outside that boundary use the same generic error
    # regardless of whether they exist, so callers cannot probe the host filesystem.
    if str(task_dir_error) == "file does not exist":
        raise task_dir_error

    # HTTP requests and other untrusted callers must never turn a submitted path
    # into a server-side file read. WebUI uploads already live in the task directory;
    # only the local CLI explicitly opts into resolving files elsewhere on the host.
    if not allow_server_file_input:
        raise ValueError(
            "custom audio file must be stored within the current task directory"
        ) from task_dir_error

    server_audio_file = path.realpath(
        requested_file
        if path.isabs(requested_file)
        else path.join(utils.root_dir(), requested_file)
    )
    if not path.isabs(requested_file):
        project_root = path.realpath(utils.root_dir())
        try:
            if path.commonpath([project_root, server_audio_file]) != project_root:
                raise ValueError(
                    "relative custom audio paths must stay within the project directory"
                )
        except ValueError as exc:
            raise ValueError(
                "custom audio file must be task-local or an existing server-side file"
            ) from exc

    if not path.isfile(server_audio_file):
        raise ValueError(
            "custom audio file does not exist or is not a file"
        ) from task_dir_error

    return server_audio_file


def _resolve_reusable_voice_preview(
    task_id: str,
    params,
    video_script: str,
    voice_preview: dict | None,
) -> tuple[str, float, object] | None:
    """
    校验并解析 WebUI 提交的完整试听缓存。

    该载荷不是公开 API 参数，只能来自当前进程的 WebUI。即便如此，后台任务
    仍重新核对文案和全部配音参数，并限制音频位于当前任务目录；任何不一致都
    回退普通 TTS，不让过期试听污染正式成片。
    """
    if not voice_preview:
        return None

    settings = generation_settings.resolve_task_settings(
        params, topic=params.video_subject
    )
    spoken_script = _represent_script(params, video_script).spoken_script
    delivery_plan = emotion_storyboard.build(
        spoken_script,
        narrator_preset=settings.narrator_preset,
        profanity_level=settings.profanity_level,
    )
    expected_synthesis_fingerprint = generation_settings.synthesis_fingerprint(
        spoken_script, settings, delivery_plan
    )

    expected_values = {
        "script": spoken_script.strip(),
        "voice_name": params.voice_name,
        "voice_rate": float(params.voice_rate),
        "voice_volume": float(params.voice_volume),
    }
    if not math.isclose(float(params.voice_volume), 1.0) or any(
        voice_preview.get(key) != value for key, value in expected_values.items()
    ) or voice_preview.get("synthesis_fingerprint") != expected_synthesis_fingerprint:
        logger.info(
            f"skip stale voice preview cache, task_id: {task_id}, "
            "reason: voice parameters changed"
        )
        return None

    preview_file = path.realpath(str(voice_preview.get("audio_file") or ""))
    task_root = path.realpath(utils.task_dir(task_id))
    try:
        preview_is_task_local = path.commonpath([task_root, preview_file]) == task_root
    except ValueError:
        preview_is_task_local = False

    duration = voice_preview.get("duration")
    sub_maker = voice_preview.get("sub_maker")
    if (
        not preview_is_task_local
        or not path.isfile(preview_file)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
        or sub_maker is None
    ):
        logger.warning(
            f"skip invalid voice preview cache, task_id: {task_id}, "
            f"audio_file: {preview_file or '<empty>'}"
        )
        return None

    logger.info(
        f"using full voice preview audio, task_id: {task_id}, duration: {duration:.2f}s"
    )
    return preview_file, math.ceil(duration), sub_maker


def _narration_cache_key(spoken_script, settings, references=(), *, params):
    delivery = emotion_storyboard.build(
        spoken_script, narrator_preset=settings.narrator_preset,
        profanity_level=settings.profanity_level,
    )
    return task_artifacts.cache_key({
        "synthesis": generation_settings.synthesis_fingerprint(spoken_script, settings, delivery),
        "voice_name": voice.parse_voice_name(params.voice_name), "voice_rate": params.voice_rate,
        "references": [hashlib.sha256(value).hexdigest() if value else None for value in references],
    })


def generate_audio(
    task_id,
    params,
    video_script,
    voice_preview=None,
    *,
    allow_server_file_input: bool = False,
    voxcpm_reference_audio: bytes | None = None,
    voxcpm_prompt_audio: bytes | None = None,
    voxcpm_prompt_text: str = "",
):
    """
    Generate audio for the video script.
    If a custom audio file is provided, it will be used directly.
    There will be no subtitle maker object returned in this case.
    Otherwise, TTS will be used to generate the audio.
    Returns:
        - audio_file: path to the generated or provided audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object if TTS is used, None otherwise
    """
    logger.info("\n\n## generating audio")
    settings = generation_settings.resolve_task_settings(
        params, topic=params.video_subject
    )
    spoken_script = _represent_script(params, video_script).spoken_script
    preflight_errors = _alignment_preflight_errors(params, spoken_script, video_script)
    if preflight_errors:
        message = "; ".join(item["message"] for item in preflight_errors)
        _mark_task_failed(
            task_id, "preflight", f"Hindi speech text is not safe to synthesize: {message}",
            details={"script": video_script, "preflight_errors": preflight_errors},
        )
        return None, None, None
    if hasattr(params, "spoken_script"):
        params.spoken_script = spoken_script
    # /audio 和 /subtitle 请求模型不包含 custom_audio_file，
    # 这里统一做兼容读取，避免直调接口时抛属性错误。
    requested_custom_audio_file = getattr(params, "custom_audio_file", None)
    try:
        custom_audio_file = resolve_custom_audio_file(
            task_id,
            requested_custom_audio_file,
            allow_server_file_input=allow_server_file_input,
        )
    except ValueError as exc:
        _mark_task_failed(
            task_id,
            "audio",
            f"invalid custom audio file: {exc}",
        )
        return None, None, None

    can_cache = voice.is_gemini_voice(getattr(params, "voice_name", "") or "") and (
        getattr(params, "subtitle_display_mode", "sentence") == "word_by_word"
        or settings.effective_visual_style == visual_style.SAVAGE_ANIMATED_EXPLAINER
    )
    audio_key = _narration_cache_key(spoken_script, settings, (
        voxcpm_reference_audio, voxcpm_prompt_audio, voxcpm_prompt_text.encode("utf-8"),
    ), params=params) if can_cache else None
    if not custom_audio_file:
        cached = task_artifacts.load_cached_artifact(task_id, "audio", audio_key) if audio_key else None
        if cached:
            duration = voice.get_audio_duration(cached["path"])
            if math.isfinite(duration) and duration > 0:
                logger.info("reusing validated narration audio without TTS: task_id={}", task_id)
                return cached["path"], math.ceil(duration), None
        reusable_preview = _resolve_reusable_voice_preview(
            task_id,
            params,
            video_script,
            voice_preview,
        )
        if reusable_preview:
            compatibility = getattr(
                reusable_preview[2], "tts_compatibility", None
            ) or generation_settings.tts_compatibility(
                settings.tts_provider, settings.tts_model
            )
            sm.state.update_task(
                task_id,
                tts_compatibility=compatibility,
                resolved_generation_settings=settings.model_dump(mode="json"),
            )
            if audio_key:
                task_artifacts.cache_artifact(task_id, "audio", audio_key, reusable_preview[0])
            return reusable_preview

        logger.info("no custom audio file provided, using TTS to generate audio.")
        audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
        tts_kwargs = {
            "text": spoken_script,
            "voice_name": voice.parse_voice_name(params.voice_name),
            "voice_rate": params.voice_rate,
            "voice_file": audio_file,
            "tts_model": settings.tts_model,
            "tts_provider": settings.tts_provider,
            "emotion_plan": emotion_storyboard.build(
                spoken_script,
                narrator_preset=settings.narrator_preset,
                profanity_level=settings.profanity_level,
            ),
        }
        if voxcpm_reference_audio is not None:
            tts_kwargs["voxcpm_reference_audio"] = voxcpm_reference_audio
        if voxcpm_prompt_audio is not None:
            tts_kwargs["voxcpm_prompt_audio"] = voxcpm_prompt_audio
            tts_kwargs["voxcpm_prompt_text"] = voxcpm_prompt_text
        sub_maker = voice.tts(**tts_kwargs)
        if sub_maker is None:
            _mark_task_failed(
                task_id,
                "audio",
                "failed to synthesize audio; verify the selected voice and TTS connectivity",
            )
            return None, None, None
        compatibility = getattr(sub_maker, "tts_compatibility", None)
        if not isinstance(compatibility, dict):
            compatibility = generation_settings.tts_compatibility(
                settings.tts_provider, settings.tts_model
            )
        sm.state.update_task(
            task_id,
            tts_compatibility=compatibility,
            resolved_generation_settings=settings.model_dump(mode="json"),
        )
        # Measure the real written audio_file, not sub_maker.cues[-1].end:
        # the latter is the last WORD BOUNDARY, and TTS leaves a fixed tail
        # past it (Edge TTS: ~0.88s at any length - 19% of a 7-word clip but
        # 1.4% of a 153-word one, so short scripts suffer most). The
        # under-count sizes paid generate_bgm() calls, is reported as
        # audio_duration to the API/WebUI, and under-sources
        # download_videos() material, scaled by video_count.
        file_duration = voice.get_audio_duration(audio_file)
        audio_duration = math.ceil(
            file_duration if file_duration > 0 else voice.get_audio_duration(sub_maker)
        )
        if audio_duration == 0:
            _mark_task_failed(task_id, "audio", "generated audio duration is zero")
            return None, None, None
        if audio_key and math.isfinite(file_duration) and file_duration > 0:
            task_artifacts.cache_artifact(task_id, "audio", audio_key, audio_file,
                                          duration=file_duration)
        return audio_file, audio_duration, sub_maker
    else:
        logger.info(f"using custom audio file: {custom_audio_file}")
        audio_duration = voice.get_audio_duration(custom_audio_file)
        if audio_duration == 0:
            _mark_task_failed(
                task_id,
                "audio",
                "custom audio duration is zero",
            )
            return None, None, None
        return custom_audio_file, audio_duration, None


def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    """
    Generate subtitle for the video script.
    If subtitle generation is disabled or no subtitle maker is provided, it will return an empty string.
    Otherwise, it will generate the subtitle using the specified provider.
    Returns:
        - subtitle_path: path to the generated subtitle file
    """
    logger.info("\n\n## generating subtitle")
    editable_script = video_script
    video_script = _represent_script(params, video_script).spoken_script
    preflight_errors = _alignment_preflight_errors(params, video_script, editable_script)
    if preflight_errors:
        _mark_task_failed(
            task_id, "preflight",
            "Hindi alignment preflight failed: " + "; ".join(item["message"] for item in preflight_errors),
            details={"preflight_errors": preflight_errors},
        )
        return ""
    if hasattr(params, "spoken_script"):
        params.spoken_script = video_script
    if not params.subtitle_enabled:
        return ""

    final_subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    if not subtitle_provider:
        logger.info("subtitle provider is empty, skip subtitle generation")
        return ""

    is_word_level = getattr(params, "subtitle_display_mode", "sentence") == "word_by_word"
    needs_audio_alignment = is_word_level and (
        voice.is_gemini_voice(getattr(params, "voice_name", "") or "")
        or generation_settings.resolve_task_settings(
            params, topic=getattr(params, "video_subject", "")
        ).tts_provider == "gemini-tts"
    )

    if sub_maker is None and subtitle_provider != "whisper" and not needs_audio_alignment:
        # 自定义音频不会经过 TTS，因此没有 Edge/Azure 等 TTS 返回的
        # sub_maker 时间轴。只有 Whisper 可以直接从音频文件转写字幕；
        # 其他字幕提供方继续保持原有行为，避免生成错误的空时间轴。
        logger.warning(
            "subtitle maker is missing, skip subtitle generation for provider: "
            f"{subtitle_provider}"
        )
        return ""

    # A failed retry must never reuse captions from an earlier narration.
    with staged_subtitle_file(final_subtitle_path) as subtitle_path:
        if needs_audio_alignment and subtitle_provider in {"edge", "whisper"}:
            # Gemini returns audio without word boundaries. Its legacy SubMaker
            # contains sentence estimates, which cannot drive word captions.
            language = (getattr(params, "video_language", "") or "").lower()
            if language.startswith("hi"):
                report_file = path.join(utils.task_dir(task_id), "alignment-quality.json")
                aligned = subtitle.align_hindi_words(
                    audio_file, video_script, subtitle_path,
                    policy=getattr(params, "hindi_alignment_policy", "balanced"),
                )
                quality = None
                if path.isfile(report_file):
                    with open(report_file, encoding="utf-8") as source:
                        quality = json.load(source)
                    sm.state.update_task(task_id, alignment_quality=quality,
                                         alignment_quality_report=report_file)
                    task_artifacts.patch_script_data(task_id, alignment_quality=quality,
                                                     alignment_quality_report=report_file)
                if not aligned:
                    # Reuse measured word boundaries as phrases when only
                    # confidence failed. If timings are structurally unsafe,
                    # try local Whisper transcription without downloading a model.
                    phrases_written = (
                        subtitle.write_phrase_cues_from_alignment(
                            quality, video_script, subtitle_path
                        ) if isinstance(quality, dict) else False
                    )
                    if not phrases_written:
                        subtitle.create(
                            audio_file=audio_file,
                            subtitle_file=subtitle_path,
                            word_level=False,
                            language="hi",
                            local_only=True,
                        )
                    has_fallback_cues = bool(subtitle.file_to_subtitles(subtitle_path))
                    params.subtitle_display_mode = "sentence"
                    fallback = {
                        "requested_mode": "word_by_word",
                        "effective_mode": "sentence" if has_fallback_cues else "none",
                        "reason": (
                            "Hindi word alignment did not meet its quality policy"
                            if has_fallback_cues else
                            "Hindi word alignment is unavailable and local transcription produced no captions"
                        ),
                    }
                    report_file = path.join(utils.task_dir(task_id), "alignment-quality.json")
                    if path.isfile(report_file):
                        fallback["quality_report"] = report_file
                    sm.state.update_task(
                        task_id,
                        subtitle_display_mode="sentence",
                        subtitle_alignment_fallback=fallback,
                    )
                    task_artifacts.patch_script_data(
                        task_id,
                        subtitle_display_mode="sentence",
                        subtitle_alignment_fallback=fallback,
                    )
                    if not has_fallback_cues:
                        logger.error("Hindi alignment and local caption fallback produced no valid cues")
                        return ""
                elif not subtitle.validate_word_cues(subtitle_path, video_script):
                    logger.error("Gemini word subtitles are unavailable or do not match narration")
                    return ""
            else:
                subtitle.create(
                    audio_file=audio_file, subtitle_file=subtitle_path,
                    word_level=True, language=None, local_only=True,
                )
                if not subtitle.validate_word_cues(subtitle_path, video_script):
                    logger.error("Gemini word subtitles are unavailable or do not match narration")
                    return ""
        elif subtitle_provider == "edge":
            voice.create_subtitle(
                text=video_script,
                sub_maker=sub_maker,
                subtitle_file=subtitle_path,
                word_level=is_word_level,
            )
            if not os.path.exists(subtitle_path):
                # Edge 字幕偶尔会因为时间轴与文案无法匹配而没有产出文件。这里不能
                # 自动切换到 Whisper，否则首次失败会在用户不知情的情况下下载数 GB
                # 的模型。只有显式配置 Whisper 时才允许加载模型，Edge 失败则保留
                # 无字幕视频并记录原因，避免意外的网络和磁盘开销。
                logger.warning(
                    "edge subtitle generation did not produce a subtitle file; "
                    "skip subtitles without falling back to whisper"
                )
                return ""

        elif subtitle_provider == "whisper":
            subtitle.create(
                audio_file=audio_file,
                subtitle_file=subtitle_path,
                word_level=is_word_level,
            )
            if not subtitle.file_to_subtitles(subtitle_path):
                logger.warning("whisper produced no usable subtitle cues")
                return ""
            if not is_word_level:
                logger.info("\n\n## correcting subtitle")
                subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

        subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
        if not subtitle_lines:
            logger.warning(f"subtitle file is invalid: {subtitle_path}")
            return ""
        os.replace(subtitle_path, final_subtitle_path)
        return final_subtitle_path


def get_video_materials(
    task_id,
    params,
    video_terms,
    audio_duration,
    loomloom_video_request: loomloom.LoomLoomConfirmedVideoRequest | None = None,
    raise_selection_errors: bool = False,
):
    if params.video_source == "local" and params.stock_video_style != asmr.SATISFYING_ASMR_CUTTING:
        logger.info("\n\n## preprocess local materials")
        materials = video.preprocess_video(
            materials=params.video_materials,
            clip_duration=params.video_clip_duration,
            image_motion=params.image_motion,
        )
        if not materials:
            _mark_task_failed(
                task_id,
                "materials",
                "no valid local video materials were found",
            )
            return None
        return [material_info.url for material_info in materials]
    elif params.video_source == "loomloom":
        if not isinstance(
            loomloom_video_request, loomloom.LoomLoomConfirmedVideoRequest
        ):
            _mark_task_failed(
                task_id,
                "materials",
                "LoomLoom video generation requires a confirmed quote",
            )
            return None

        request = loomloom_video_request
        logger.info(
            "\n\n## generating "
            f"{len(request.batch.input_rows)} video materials with LoomLoom"
        )
        run_id = ""
        try:
            request.validate()
            backend = loomloom.LoomLoomVideoBackend(request.settings)
            execution = backend.execute(
                request.batch,
                client_request_id=request.client_request_id,
                listing_version_id=request.listing_version_id,
                confirm=True,
            )
            run_id = execution.run_id
            # execute 返回即表示付费任务已经由远端接受。必须先把 run ID 写入
            # 进程日志，即使 Redis 等状态后端随后不可用，运维人员仍能凭日志
            # 在胜算云侧定位任务，不能让唯一标识只存在于局部变量中。
            logger.info(
                "LoomLoom paid video run created: "
                f"task_id={task_id}, run_id={run_id}, "
                f"listing_version_id={request.listing_version_id}"
            )
            # 付费任务一旦创建就立即记录远端 ID。即使后续轮询超时，日志和任务
            # 状态仍能帮助用户或平台支持人员定位并找回已经生成的产物。状态后端
            # 故障只能降低可观测性，不能中断已经开始计费的远端任务和产物下载。
            _record_loomloom_run_reference(
                task_id=task_id,
                run_id=run_id,
                listing_version_id=request.listing_version_id,
            )
            backend.wait_for_run(run_id)
            return list(
                backend.download_video_results(
                    run_id,
                    utils.task_dir(task_id),
                )
            )
        except (loomloom.LoomLoomError, ValueError) as exc:
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details={
                    "loomloom_run_id": run_id,
                    "loomloom_listing_version_id": request.listing_version_id,
                },
            )
            return None
    else:
        logger.info(f"\n\n## downloading videos from {params.video_source}")
        # 顺序匹配模式只在用户显式开启时生效。这里强制素材下载按关键词顺序
        # 轮询，避免某个早期关键词下载太多素材，把后续脚本主题挤出最终时间线。
        try:
            downloaded_videos = material.download_videos(
                task_id=task_id,
                search_terms=video_terms,
                source=params.video_source,
                video_aspect=params.video_aspect,
                video_concat_mode=(
                    VideoConcatMode.sequential
                    if params.match_materials_to_script
                    and params.stock_video_style != asmr.SATISFYING_ASMR_CUTTING
                    else params.video_concat_mode
                ),
                audio_duration=audio_duration * params.video_count,
                max_clip_duration=params.video_clip_duration,
                match_script_order=(
                    params.match_materials_to_script
                    and params.stock_video_style != asmr.SATISFYING_ASMR_CUTTING
                ),
                stock_video_style=params.stock_video_style,
                asmr_categories=params.asmr_categories,
                require_relevant_coverage=(
                    params.effective_visual_style == visual_style.RELEVANT_STOCK
                ),
                clip_speed=params.video_clip_speed,
                image_motion=params.image_motion,
                # 素材阶段占用 40%~50%：每下完一个文件推进一次，慢速网络下
                # 进度条不再长时间停在 40%。
                progress_callback=_stage_progress_reporter(task_id, 40, 50),
            )
        except material.NoRelevantAsmrMaterialsError as exc:
            if raise_selection_errors:
                raise
            _mark_task_failed(task_id, "materials", str(exc))
            return None
        except material.InsufficientRelevantStockError as exc:
            if raise_selection_errors:
                raise
            _mark_task_failed(task_id, "materials", str(exc))
            return None
        except volcengine_seedance.VolcEngineSeedanceError as exc:
            # 未确认状态和已生成但下载失败都对应一个可在方舟控制台恢复的远端
            # 任务。统一从异常携带的 task_id 写入失败状态，避免不同异常分支
            # 各自维护恢复信息并在后续扩展时再次遗漏。
            remote_task_id = str(getattr(exc, "task_id", "") or "").strip()
            details = (
                {"volcengine_seedance_task_id": remote_task_id}
                if remote_task_id
                else None
            )
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details=details,
            )
            return None
        except (
            material.WaveSpeedUnconfirmedTaskError,
            material.WaveSpeedDownloadError,
        ) as exc:
            # Once a paid prediction exists, a polling or download failure must
            # leave its ID in task state. Earlier successful clips cannot turn
            # this incomplete run into an apparently completed video task.
            prediction_id = str(getattr(exc, "prediction_id", "") or "").strip()
            details = {"wavespeed_prediction_id": prediction_id} if prediction_id else None
            _mark_task_failed(task_id, "materials", str(exc), details=details)
            return None
        except material.OpenAIImagePaidResultError as exc:
            # A paid request with an uncertain response, failed result download,
            # or failed local render must stop before another image order.
            _mark_task_failed(task_id, "materials", str(exc))
            return None
        except ofox.OFoxError as exc:
            # 与方舟同一恢复语义：未确认状态和已生成但下载失败都对应一个可在
            # OFox 控制台恢复的远端任务，统一从异常携带的 task_id 写入失败状态。
            remote_task_id = str(getattr(exc, "task_id", "") or "").strip()
            details = (
                {"ofox_task_id": remote_task_id} if remote_task_id else None
            )
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details=details,
            )
            return None
        except metaso_minimax.MetasoMiniMaxError as exc:
            # 秘塔任务与方舟任务使用不同的恢复入口和字段名，不能合并成一个
            # 模糊的 remote_task_id。保留明确 Provider 前缀便于 API、WebUI
            # 和运维日志直接定位对应平台。
            remote_task_id = str(getattr(exc, "task_id", "") or "").strip()
            details = (
                {"metaso_minimax_task_id": remote_task_id} if remote_task_id else None
            )
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details=details,
            )
            return None
        except muapi.MuAPIError as exc:
            # MuAPI tasks are billable after acceptance.  Preserve the remote
            # request ID on any ambiguous or post-completion failure so the
            # user can recover it from the provider dashboard.
            remote_task_id = str(getattr(exc, "task_id", "") or "").strip()
            details = {"muapi_task_id": remote_task_id} if remote_task_id else None
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details=details,
            )
            return None
        if not downloaded_videos:
            _mark_task_failed(
                task_id,
                "materials",
                f"failed to download video materials from {params.video_source}",
            )
            return None
        return downloaded_videos


def _record_loomloom_run_reference(
    *, task_id: str, run_id: str, listing_version_id: str
) -> bool | None:
    """
    尽最大努力保存已创建的付费 LoomLoom Run，不让状态故障中断远端任务。

    返回 True 表示保存成功，False 表示任务记录已经不存在，None 表示状态后端
    在有限重试后仍不可用。调用方无论得到哪种结果都应继续轮询和下载，因为
    execute 已经产生外部付费副作用，停止本地流程只会让产物更难找回。
    """
    fields = {
        "loomloom_run_id": run_id,
        "loomloom_listing_version_id": listing_version_id,
    }
    for attempt in range(1, _LOOMLOOM_STATE_WRITE_ATTEMPTS + 1):
        try:
            updated = sm.state.patch_task(task_id, **fields)
        except Exception as exc:
            if attempt >= _LOOMLOOM_STATE_WRITE_ATTEMPTS:
                logger.exception(
                    "failed to persist LoomLoom paid run after retries: "
                    f"task_id={task_id}, run_id={run_id}, attempts={attempt}, "
                    f"error={exc}"
                )
                return None
            logger.warning(
                "retry LoomLoom paid run state update: "
                f"task_id={task_id}, run_id={run_id}, attempt={attempt}, "
                f"error={exc}"
            )
            time.sleep(_LOOMLOOM_STATE_RETRY_DELAY_SECONDS)
            continue

        if updated is False:
            logger.warning(
                "could not persist LoomLoom paid run because task is missing: "
                f"task_id={task_id}, run_id={run_id}"
            )
        return updated

    return None


def _get_material_source_groups(task_id: str, video_paths: list[str]) -> dict[str, str]:
    """Recover keyword groups from the downloaded material manifest."""
    try:
        with open(path.join(utils.task_dir(task_id), "script.json"), encoding="utf-8") as file:
            payload = json.load(file)
        groups = {
            record["local_file"]: record["search_term"]
            for record in payload.get("material_sources", [])
            if isinstance(record, dict)
            and isinstance(record.get("local_file"), str)
            and isinstance(record.get("search_term"), str)
            and record["search_term"]
        }
        return {
            file: groups[path.basename(file)]
            for file in video_paths
            if path.basename(file) in groups
        }
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        logger.warning(f"Cannot read material keyword groups: task_id={task_id}, error={exc}")
        return {}


def generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path, audio_duration,
    *, partial_results: dict | None = None,
    branding_video_segments: list[dict] | None = None,
):
    """Record completed outputs even if a later clip raises before returning."""
    final_video_paths = []
    combined_video_paths = []
    warnings = []
    if partial_results is not None:
        # The caller owns this snapshot so it can report completed files even
        # when the state backend fails during a progress/checkpoint write.
        partial_results.update(
            videos=final_video_paths, combined_videos=combined_video_paths,
            warnings=warnings,
        )
    allocate_batch_materials = params.video_count > 1 and params.video_source in {
        "pexels", "pixabay", "coverr", "local"
    }
    source_usage = {}
    material_selections = []
    source_groups = (
        _get_material_source_groups(task_id, downloaded_videos)
        if (allocate_batch_materials and params.match_materials_to_script
            and params.stock_video_style != asmr.SATISFYING_ASMR_CUTTING
            and params.video_source != "local")
        else {}
    )
    video_music_provider = _VIDEO_MUSIC_PROVIDERS.get(params.bgm_type)
    video_music_requested = (
        video_music_provider is not None
        and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
    )
    # Matching preserves keyword order; batch allocation varies each keyword's candidates.
    if (params.match_materials_to_script
            and params.stock_video_style != asmr.SATISFYING_ASMR_CUTTING):
        video_concat_mode = VideoConcatMode.sequential
    elif params.video_count == 1:
        video_concat_mode = params.video_concat_mode
    else:
        video_concat_mode = VideoConcatMode.random
    video_transition_mode = params.video_transition_mode

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        if partial_results is not None:
            partial_results["failed_video_index"] = index
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        used_video_paths = []
        batch_options = (
            {
                "source_usage": source_usage,
                "source_groups": source_groups,
                "used_video_paths": used_video_paths,
            }
            if allocate_batch_materials else {}
        )
        # 每条视频占 50% 进度中的一份，合成与最终编码各占一半。片段处理是
        # 合成里最耗时的部分，逐段推进这一半的进度。
        combine_share = 50 / params.video_count / 2
        video.combine_videos(
            combined_video_path=combined_video_path,
            video_paths=downloaded_videos,
            audio_file=audio_file,
            video_aspect=params.video_aspect,
            video_fit_mode=params.video_fit_mode,
            video_concat_mode=video_concat_mode,
            video_transition_mode=video_transition_mode,
            max_clip_duration=(
                max(params.video_clip_duration, math.ceil(audio_duration) + 1)
                if params.effective_visual_style == visual_style.SAVAGE_ANIMATED_EXPLAINER
                else params.video_clip_duration
            ),
            threads=params.n_threads,
            clip_speed=params.video_clip_speed,
            # Requested target duration is not permission to repeat unrelated
            # footage; material coverage was requested against measured audio.
            allow_shortage_loop=False,
            progress_callback=_stage_progress_reporter(
                task_id, _progress, _progress + combine_share
            ),
            **batch_options,
        )
        if allocate_batch_materials:
            selected_sources = list(dict.fromkeys(used_video_paths))
            reused_sources = [file for file in selected_sources if source_usage.get(file, 0)]
            for file in selected_sources:
                source_usage[file] = source_usage.get(file, 0) + 1
            material_selections.append({
                "video_index": index,
                "local_files": [path.basename(file) for file in used_video_paths],
                "reused_files": [path.basename(file) for file in reused_sources],
            })
            task_artifacts.patch_script_data(task_id, material_selections=material_selections)
            logger.info(
                f"Batch material allocation: video_index={index}, "
                f"sources={len(selected_sources)}, reused_sources={len(reused_sources)}"
            )
            if reused_sources:
                warnings.append({
                    "code": "batch_materials_reused",
                    "video_index": index,
                    "count": len(reused_sources),
                })

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        # 视频配乐模式先明确禁用默认 BGM 解析，避免旧任务残留的 bgm_file 被
        # 误用。只有音量大于 0 才生成代理并调用付费 API；0 音量统一跳过。
        bgm_file_override = "" if video_music_provider else None
        if video_music_requested:
            service = video_music_provider["service"]
            display_name = video_music_provider["display_name"]
            warning_code = video_music_provider["warning_code"]
            generated_bgm_path = path.join(
                utils.task_dir(task_id),
                (f"{params.bgm_type}-bgm-{index}{video_music_provider['suffix']}"),
            )
            try:
                service.generate_bgm(
                    video_path=combined_video_path,
                    output_path=generated_bgm_path,
                    video_duration=audio_duration,
                    prompt=_get_video_music_prompt(params),
                )
                bgm_file_override = generated_bgm_path
            except video_music_provider["error_type"] as exc:
                # 视频、旁白和字幕都已生成时，第三方配乐临时失败不应浪费整条
                # 任务。当前视频明确禁用 BGM，并把降级结果返回 WebUI 提醒用户。
                logger.warning(
                    f"{display_name} BGM generation failed: task_id={task_id}, "
                    f"video_index={index}, error={exc}"
                )
                bgm_file_override = ""
                warnings.append({"code": warning_code, "video_index": index})

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        bgm_mix_succeeded = video.generate_video(
            video_path=combined_video_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=final_video_path,
            params=params,
            bgm_file_override=bgm_file_override,
            branding_segments=(branding_video_segments or [])
            if params.effective_visual_style != visual_style.SAVAGE_ANIMATED_EXPLAINER
            else [],
        )
        if (
            video_music_provider is not None
            and bgm_file_override
            and not bgm_mix_succeeded
        ):
            # 第三方已成功返回并通过 FFmpeg 校验，但 MoviePy 最终混音仍可能
            # 因运行环境失败。视频服务会保留无 BGM 成片；API 生成失败时
            # override 为空，因此不会重复追加警告。
            warnings.append(
                {
                    "code": video_music_provider["warning_code"],
                    "video_index": index,
                }
            )

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)
        _progress += 50 / params.video_count / 2
        sm.state.update_task(
            task_id, progress=_progress, videos=list(final_video_paths),
            combined_videos=list(combined_video_paths), warnings=list(warnings) or None,
        )

    return final_video_paths, combined_video_paths, warnings


def _patch_cross_post_state(task_id: str, **kwargs) -> bool | None:
    """安全更新发布字段；短暂状态后端故障时有限重试。"""
    for attempt in range(1, _CROSS_POST_STATE_WRITE_ATTEMPTS + 1):
        try:
            return sm.state.patch_task(task_id, **kwargs)
        except Exception as exc:
            # Redis 短暂断连不应让任务永久停留在 pending/processing。发布状态
            # 写入频率很低，这里使用固定次数和短等待即可覆盖瞬时故障，同时
            # 避免后台线程无限阻塞。最后一次失败保留完整堆栈便于定位。
            if attempt >= _CROSS_POST_STATE_WRITE_ATTEMPTS:
                logger.exception(
                    f"failed to update cross-post state after retries, "
                    f"task_id: {task_id}, fields: {', '.join(kwargs)}, "
                    f"attempts: {attempt}, error: {exc}"
                )
                return None

            logger.warning(
                f"retry cross-post state update, task_id: {task_id}, "
                f"fields: {', '.join(kwargs)}, attempt: {attempt}, error: {exc}"
            )
            time.sleep(_CROSS_POST_STATE_RETRY_DELAY_SECONDS)

    return None


def _record_cross_post_failure(
    task_id: str,
    error: Exception,
    results: list[dict] | None = None,
) -> None:
    """尽最大努力保存发布失败；状态后端不可用时由日志保留诊断信息。"""
    updated = _patch_cross_post_state(
        task_id,
        cross_post_state=const.CROSS_POST_STATE_FAILED,
        cross_post_results=results or None,
        cross_post_error=str(error),
        cross_post_owner=None,
    )
    if updated is False:
        logger.warning(f"discard cross-post failure for missing task: {task_id}")


def _ensure_cross_post_terminal_state(task_id: str) -> None:
    """Future 结束后把仍处于活动态的任务收敛为失败。"""
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        # 此处已经是 Future 的最终回调，没有后续同步调用方可以处理异常。
        # 状态后端恢复后，下一次进程启动仍会通过恢复逻辑处理遗留状态。
        logger.exception(
            f"failed to verify final cross-post state, task_id: {task_id}, error: {exc}"
        )
        return

    if not task or task.get("cross_post_state") not in _ACTIVE_CROSS_POST_STATES:
        return

    logger.warning(
        f"cross-post worker ended without terminal state, task_id: {task_id}, "
        f"state: {task.get('cross_post_state')}"
    )
    _record_cross_post_failure(
        task_id,
        RuntimeError("cross-post worker ended without persisting a terminal state"),
        task.get("cross_post_results"),
    )


def recover_interrupted_cross_posts(page_size: int = 100) -> int | None:
    """
    将进程重启后无法恢复的发布任务标记为失败。

    跨平台发布使用当前进程内的线程池，不是持久化任务队列。进程启动时，
    Redis 中残留的 pending/processing 不会自动继续执行；如果继续把它们视为
    运行中，用户将永久无法删除任务。这里仅收集一次任务 ID，再读取当前状态；
    不能跨多次 Redis SCAN 按页码推进，否则扫描顺序变化会漏掉遗留任务。
    只处理当前进程没有对应 Future 的活动记录，并保留已经生成的视频结果。
    """
    recovered = 0
    try:
        task_ids = sm.state.list_task_ids(scan_count=page_size)
        for task_id in task_ids:
            # 扫描之后任务可能已被删除或转为终态；以最新状态决定是否恢复，
            # 不能使用扫描时的旧快照覆盖已经完成的发布结果。
            task = sm.state.get_task(task_id)
            if (
                not task
                or task.get("cross_post_state") not in _ACTIVE_CROSS_POST_STATES
                or _is_cross_post_active_in_process(task_id)
                or _is_cross_post_owner_alive(task.get("cross_post_owner"))
            ):
                continue

            updated = _patch_cross_post_state(
                task_id,
                cross_post_state=const.CROSS_POST_STATE_FAILED,
                cross_post_error=_INTERRUPTED_CROSS_POST_ERROR,
                cross_post_owner=None,
            )
            if updated is True:
                recovered += 1
    except Exception as exc:
        logger.exception(f"failed to recover interrupted cross-post tasks: {exc}")
        return None

    if recovered:
        logger.warning(f"recovered interrupted cross-post tasks: {recovered}")
    return recovered


def _social_metadata_language(params: VideoParams) -> str:
    language = str(getattr(params, "youtube_caption_language", "English") or "English").casefold()
    return "Hindi" if language in {"hi", "hindi"} else "English"


def _youtube_caption_context_for_params(params: VideoParams) -> str:
    branding = getattr(params, "channel_branding", {}) or {}

    def brand_segment(name: str) -> dict:
        segment = branding.get(name, {}) if isinstance(branding, dict) else {}
        segment = segment if isinstance(segment, dict) else {}
        return {
            "enabled": bool(segment.get("enabled")),
            "text": str(segment.get("text") or "")[:240],
            "audio_present": bool(segment.get("audio")),
        }

    context = {
        "caption_language": _social_metadata_language(params),
        "voice_name": str(getattr(params, "voice_name", "") or ""),
        "narrator_preset": str(getattr(params, "narrator_preset", "") or ""),
        "voice_rate": getattr(params, "voice_rate", 1.0),
        "visual_style": str(getattr(params, "effective_visual_style", "") or
                             getattr(params, "visual_style", "") or ""),
        "profanity_level": str(getattr(params, "profanity_level", "") or ""),
        "intro": brand_segment("intro"),
        "outro": brand_segment("outro"),
    }
    return json.dumps(context, ensure_ascii=False, sort_keys=True)[:1500]


def _youtube_caption_source_for_params(params: VideoParams, narration: str) -> str:
    """Build caption-only context without changing the canonical TTS narration."""
    branding = getattr(params, "channel_branding", {}) or {}
    intro = ""
    outro = ""
    for name, label in (("intro", "Channel intro"), ("outro", "Channel outro")):
        segment = branding.get(name, {}) if isinstance(branding, dict) else {}
        if isinstance(segment, dict) and segment.get("enabled") and str(segment.get("text") or "").strip():
            value = f"{label}: {str(segment['text']).strip()[:500]}"
            if name == "intro":
                intro = value
            else:
                outro = value
    segments = [intro, f"Final narration: {str(narration or '').strip()}", outro]
    segments = [segment for segment in segments if segment]
    return "\n".join(segments)


def _generate_completed_video_youtube_metadata(
    task_id: str, params: VideoParams, narration: str, video_paths: list[str],
) -> list[dict]:
    """Generate narration-language YouTube copy for each completed output."""
    channel_context = _youtube_caption_context_for_params(params)
    caption_source = _youtube_caption_source_for_params(params, narration)
    narration_fingerprint = hashlib.sha256(narration.encode("utf-8")).hexdigest()
    caption_source_fingerprint = hashlib.sha256(caption_source.encode("utf-8")).hexdigest()
    generated = []
    for index, video_path in enumerate(video_paths, start=1):
        try:
            metadata = llm.generate_social_metadata(
                video_subject=params.video_subject,
                video_script=caption_source,
                language=_social_metadata_language(params),
                platform="youtube_shorts",
                channel_context=channel_context,
                max_attempts=2,
            )
        except Exception as exc:
            logger.warning(
                "YouTube metadata generation failed after video completion: "
                "task_id={}, video_index={}, error_type={}",
                task_id, index, type(exc).__name__,
            )
            continue
        if not isinstance(metadata, dict):
            continue
        generated.append({
            "video_index": index,
            "video_name": os.path.basename(video_path),
            "narration_fingerprint": narration_fingerprint,
            "caption_source_fingerprint": caption_source_fingerprint,
            "title": str(metadata.get("title") or "")[:100],
            "caption": str(metadata.get("caption") or ""),
            "hashtags": list(metadata.get("hashtags") or []),
            "channel_context_fingerprint": hashlib.sha256(
                channel_context.encode("utf-8")
            ).hexdigest(),
        })
    return generated


def _run_cross_post(
    task_id: str,
    video_paths: tuple[str, ...],
    video_subject: str,
    video_script: str,
    video_language: str,
    platforms: tuple[str, ...],
    youtube_privacy_status: str,
    youtube_made_for_kids: bool = False,
    upload_account: dict | None = None,
    unverified_publication_label: bool = False,
) -> None:
    """后台执行跨平台发布，并只补充发布相关的任务字段。"""
    results = []
    try:
        state_updated = _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_PROCESSING,
            cross_post_error=None,
            cross_post_owner=_cross_post_process_owner,
        )
        if state_updated is not True:
            # False 表示任务已删除，None 表示状态后端暂时不可用。两种情况都
            # 不应继续调用第三方接口，否则用户无法查询或控制这次发布。
            if state_updated is False:
                logger.warning(f"skip cross-post for missing task: {task_id}")
            else:
                _record_cross_post_failure(
                    task_id,
                    RuntimeError("failed to persist cross-post processing state"),
                )
            return

        logger.info(
            f"cross-post started, task_id: {task_id}, platforms: {', '.join(platforms)}"
        )
        task_snapshot = sm.state.get_task(task_id) or {}
        saved_metadata = {
            int(item.get("video_index", 0)): item
            for item in task_snapshot.get("youtube_metadata_by_video", [])
            if isinstance(item, dict) and str(item.get("video_index", "")).isdigit()
        }
        has_youtube = any(platform.startswith("youtube") for platform in platforms)
        social_platform = "youtube_shorts"
        if platforms and not has_youtube:
            first = (platforms[0] or "").strip().lower()
            social_platform = _CROSS_POST_SOCIAL_PLATFORMS.get(first, first)

        shared_generated_metadata = None
        for video_index, video_path in enumerate(video_paths, start=1):
            youtube_extra = None
            post_title = video_subject or "Check out this video! #shorts #viral"
            if platforms:
                metadata = saved_metadata.get(video_index) if has_youtube else None
                if metadata is None:
                    if shared_generated_metadata is None:
                        shared_generated_metadata = llm.generate_social_metadata(
                            video_subject=video_subject,
                            video_script=video_script,
                            language="Hindi" if str(video_language or "").casefold().startswith("hi") else "English",
                            platform=social_platform,
                        )
                    metadata = shared_generated_metadata
                if has_youtube:
                    youtube_title = metadata.get("title", video_subject)
                    youtube_description = metadata.get("caption", "")
                    if unverified_publication_label:
                        youtube_title = ("[UNVERIFIED FACTS] " + str(youtube_title))[:100]
                        youtube_description = (
                            "Disclosure: MPT did not verify the factual claims in this video against sources.\n\n"
                            + str(youtube_description)
                        )[:5000]
                    youtube_extra = {
                        "youtube_title": youtube_title,
                        "youtube_description": youtube_description,
                        "tags": metadata.get("hashtags", []),
                        "privacyStatus": youtube_privacy_status,
                        "selfDeclaredMadeForKids": youtube_made_for_kids,
                        "containsSyntheticMedia": True,
                    }
                post_title = (
                    metadata.get("caption")
                    or metadata.get("title")
                    or video_subject
                    or "Check out this video! #shorts #viral"
                )
                if unverified_publication_label and not has_youtube:
                    post_title = ("[UNVERIFIED FACTS] " + str(post_title))[:100]

            pending_result_index = None

            def record_background_request(request_id: str) -> None:
                nonlocal pending_result_index
                # Persist the remote handle before polling. If this process
                # exits mid-upload, recovery can expose the ID to the user.
                pending_result_index = len(results)
                results.append(
                    {
                        "video_index": pending_result_index + 1,
                        "request_id": request_id,
                        "status": "processing",
                    }
                )
                if _patch_cross_post_state(
                    task_id, cross_post_results=list(results)
                ) is not True:
                    logger.warning(
                        "could not persist background upload request ID: "
                        f"task_id={task_id}, request_id={request_id}"
                    )

            upload_kwargs = {}
            if upload_account is not None:
                upload_kwargs["account"] = upload_account
            result = upload_post.cross_post_video(
                video_path=video_path,
                title=post_title,
                platforms=list(platforms),
                youtube_extra=youtube_extra,
                on_background_start=record_background_request,
                **upload_kwargs,
            )
            if not isinstance(result, dict):
                result = {
                    "success": False,
                    "error": "Upload-Post returned an invalid response",
                }
            if pending_result_index is None:
                results.append(result)
            else:
                results[pending_result_index] = result

        failures = [result for result in results if not result.get("success")]
        if failures:
            error_messages = [
                str(
                    result.get("error")
                    or result.get("message")
                    or "unknown upload error"
                )
                for result in failures
            ]
            cross_post_state = const.CROSS_POST_STATE_FAILED
            cross_post_error = "; ".join(error_messages)
            logger.warning(
                f"cross-post completed with failures, task_id: {task_id}, "
                f"failed: {len(failures)}, total: {len(results)}"
            )
        else:
            cross_post_state = const.CROSS_POST_STATE_COMPLETE
            cross_post_error = None
            logger.success(
                f"cross-post completed, task_id: {task_id}, videos: {len(results)}"
            )

        state_updated = _patch_cross_post_state(
            task_id,
            cross_post_state=cross_post_state,
            cross_post_results=results,
            cross_post_error=cross_post_error,
            cross_post_owner=None,
        )
        if state_updated is False:
            logger.warning(f"discard cross-post result for missing task: {task_id}")
        elif state_updated is None:
            # 上传已经结束但结果没有持久化时，不能继续保留 processing。
            # 失败状态写入会再次经过有限重试，至少让调用方得到明确终态。
            _record_cross_post_failure(
                task_id,
                RuntimeError("failed to persist final cross-post result"),
                results,
            )
    except Exception as exc:
        # 发布失败只影响发布状态，不能反向覆盖已经完成的视频任务。
        # 异常原文写入任务状态，API 调用方无需访问服务端日志也能定位问题。
        logger.exception(f"cross-post failed, task_id: {task_id}, error: {exc}")
        _record_cross_post_failure(task_id, exc, results)


def _run_cross_post_with_slot(*args) -> None:
    """执行发布任务，并确保成功、失败或异常时都会归还队列容量。"""
    try:
        _run_cross_post(*args)
    except Exception as exc:
        # _run_cross_post 已处理预期异常；这里是最后一道保护，避免未来新增
        # 逻辑抛出的异常只保存在无人读取的 Future 中。
        task_id = str(args[0]) if args else "unknown"
        logger.exception(f"cross-post worker crashed, task_id: {task_id}, error: {exc}")
        if args:
            _record_cross_post_failure(task_id, exc)
    finally:
        _cross_post_slots.release()


def _finalize_cross_post_future(task_id: str, future: Future) -> None:
    """清理 Future 注册，并确保取消、异常和状态写入失败都能收敛。"""
    _unregister_cross_post_future(task_id, future)

    try:
        error = future.exception()
    except CancelledError:
        logger.warning(f"cross-post future was cancelled, task_id: {task_id}")
        # Future 在开始执行前被取消时，worker 的 finally 不会运行，因此需要
        # 在回调中归还队列容量，并把持久化状态改为失败。
        _cross_post_slots.release()
        _record_cross_post_failure(
            task_id,
            RuntimeError("cross-post job was cancelled before execution"),
        )
        return
    except Exception as exc:
        logger.exception(
            f"failed to inspect cross-post future, task_id: {task_id}, error: {exc}"
        )
        _ensure_cross_post_terminal_state(task_id)
        return

    if error is not None:
        logger.error(
            f"cross-post future failed, task_id: {task_id}, "
            f"error: {type(error).__name__}: {error}"
        )

    _ensure_cross_post_terminal_state(task_id)


def _schedule_cross_post(
    task_id: str,
    video_paths: list[str],
    params: VideoParams,
    video_script: str,
    platforms: list[str],
    youtube_privacy_status: str,
    youtube_made_for_kids: bool = False,
    unverified_publication_label: bool = False,
) -> str | None:
    """提交后台发布任务；成功返回 None，调度失败返回可查询的错误原因。"""
    if not _cross_post_slots.acquire(blocking=False):
        error = "cross-post queue is full; publishing was skipped"
        logger.warning(
            f"skip cross-post because queue is full, task_id: {task_id}, "
            f"capacity: {_cross_post_max_pending_tasks}"
        )
        _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_FAILED,
            cross_post_error=error,
            cross_post_owner=None,
        )
        return error

    try:
        future = _cross_post_executor.submit(
            _run_cross_post_with_slot,
            task_id,
            tuple(video_paths),
            params.video_subject or "",
            video_script,
            params.video_language or "",
            tuple(platforms),
            youtube_privacy_status,
            youtube_made_for_kids,
            upload_post.upload_post_service.snapshot_account(),
            unverified_publication_label,
        )
        _register_cross_post_future(task_id, future)
        future.add_done_callback(partial(_finalize_cross_post_future, task_id))
    except RuntimeError as exc:
        _unregister_cross_post_future(task_id)
        _cross_post_slots.release()
        logger.exception(
            f"failed to schedule cross-post, task_id: {task_id}, error: {exc}"
        )
        _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_FAILED,
            cross_post_error=f"failed to schedule cross-post: {exc}",
            cross_post_owner=None,
        )
        return f"failed to schedule cross-post: {exc}"

    return None


def _run_pipeline(
    task_id,
    params: VideoParams,
    stop_at: str = "video",
    voice_preview: dict | None = None,
    loomloom_video_request: loomloom.LoomLoomConfirmedVideoRequest | None = None,
    allow_server_file_input: bool = False,
    voxcpm_reference_audio: bytes | None = None,
    voxcpm_prompt_audio: bytes | None = None,
    voxcpm_prompt_text: str = "",
    suppress_auto_upload: bool = False,
    reuse_saved_audio: bool = False,
):
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    settings = generation_settings.resolve_task_settings(
        params, topic=params.video_subject
    )
    # Pin AUTO's concrete choice before any style-specific prompt or synthesis.
    params.effective_visual_style = settings.effective_visual_style
    params.stock_video_style = (
        asmr.SATISFYING_ASMR_CUTTING
        if settings.effective_visual_style == visual_style.ASMR_CUTTING
        else "default"
    )
    sm.state.update_task(
        task_id, state=const.TASK_STATE_PROCESSING, progress=5,
        failed_stage=None, error=None,
        videos=[], combined_videos=[], warnings=None, failed_video_index=None,
        resolved_generation_settings=settings.model_dump(mode="json"),
    )

    if stop_at == "video" and params.subtitle_enabled:
        try:
            video.validate_subtitle_colors(params)
        except ValueError as exc:
            return _mark_task_failed(task_id, "preflight", str(exc))

    if stop_at in {"materials", "video"} and params.video_source not in _SUPPORTED_VIDEO_SOURCES:
        return _mark_task_failed(
            task_id, "preflight", f"unsupported video source: {params.video_source!r}"
        )

    if (stop_at in {"materials", "video"}
            and params.stock_video_style == asmr.SATISFYING_ASMR_CUTTING
            and params.video_source not in {"pexels", "pixabay", "local"}):
        return _mark_task_failed(
            task_id, "preflight",
            "Satisfying ASMR Cutting requires Pexels, Pixabay, or a reviewed local ASMR library.",
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "volcengine_seedance"
        and not volcengine_seedance.is_enabled()
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "Volcano Engine Seedance requires an Ark API key",
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "ofox"
        and not ofox.is_enabled()
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "OFox video generation requires an OFox API key",
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "metaso_minimax"
        and not metaso_minimax.is_enabled()
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "Metaso MiniMax requires an API key",
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "muapi"
        and not muapi.is_enabled()
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "MuAPI video generation requires a MuAPI API key",
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "openai_image"
        and not material.is_openai_image_enabled(
            config.snapshot_config_with_pending(config.app)
        )
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "OpenAI image source requires openai_image_base_url and "
            "openai_image_model in config.toml (openai_image_api_keys is "
            "optional for local gateways that need no auth)",
        )

    # 只有完整成片流程需要视频配乐供应商。尽早阻止缺少 Key 的完整任务，避免
    # 先消耗 LLM、TTS 和素材服务额度；中间产物接口仍可独立使用。
    video_music_provider = _VIDEO_MUSIC_PROVIDERS.get(params.bgm_type)
    video_music_enabled = (
        stop_at == "video"
        and video_music_provider is not None
        and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
    )
    if video_music_enabled:
        service = video_music_provider["service"]
        display_name = video_music_provider["display_name"]
        if not service.is_enabled():
            return _mark_task_failed(
                task_id,
                "preflight",
                f"{display_name} background music requires an API key",
            )

        # WebUI 会限制输入长度，但 API、CLI 和历史任务可以绕过前端控件。
        # 在生成脚本、配音和素材之前按供应商上限再次校验，避免完整视频合成后
        # 才由第三方请求拒绝。服务层仍保留同一校验，作为直接调用时的最后防线。
        music_prompt = _get_video_music_prompt(params)
        max_prompt_length = int(getattr(service, "MAX_PROMPT_LENGTH", 0) or 0)
        if max_prompt_length and len(music_prompt) > max_prompt_length:
            return _mark_task_failed(
                task_id,
                "preflight",
                (f"{display_name} music prompt exceeds {max_prompt_length} characters"),
            )

        # 供应商可以选择提供不计费的账号前置检查。检查函数只应抛出确定性
        # 错误；网络波动或权限范围无法确认时由服务层记录警告并继续实际生成。
        validate_access = getattr(service, "validate_generation_access", None)
        if callable(validate_access):
            try:
                validate_access()
            except video_music_provider["error_type"] as exc:
                return _mark_task_failed(task_id, "preflight", str(exc))

    # 只有 script/terms 中间产物不需要 FFmpeg（它们不生成音频或视频）。API、
    # CLI 和 WebUI 都通过这个共享入口执行任务，因此在此统一探测，而不是
    # 分别在各个入口重复检查，能保证三条路径的行为一致。放在配乐 Key 校验
    # 之后，是为了不改变那些校验原有的"最先失败"顺序和错误信息。
    if stop_at not in ("script", "terms") and not utils.check_ffmpeg_ready():
        return _mark_task_failed(
            task_id,
            "preflight",
            "ffmpeg is not available; install ffmpeg or set app.ffmpeg_path "
            "in config.toml to a working ffmpeg executable",
        )

    saved_manifest = None
    if reuse_saved_audio:
        try:
            with open(path.join(utils.task_dir(task_id), "script.json"), encoding="utf-8") as source:
                saved_manifest = json.load(source)
            if saved_manifest.get("script_quality_accepted") is False:
                raise ValueError("saved script draft failed quality checks and requires editing")
            video_script = str(saved_manifest.get("script") or "")
            if not video_script:
                raise ValueError("saved task manifest has no editable script")
        except (OSError, ValueError, TypeError) as exc:
            return _mark_task_failed(task_id, "resume", f"saved script could not be loaded: {exc}")
        params.video_script = video_script
        params.spoken_script = str(saved_manifest.get("spoken_script") or "")
        if not params.spoken_script:
            params.spoken_script = _represent_script(params, video_script).spoken_script
        for field in (
            "fact_claims", "fact_evidence", "fact_verification_state",
            "fact_verification_policy", "fact_verification_script_hash",
            "fact_verification_report",
        ):
            if field in saved_manifest and hasattr(params, field):
                setattr(params, field, saved_manifest[field])
        video_terms = saved_manifest.get("search_terms", "")
        logger.info("resuming from saved script and narration audio: task_id={}", task_id)
    else:
        video_script = generate_script(task_id, params)
    # The LLM adapter uses a leading "Error: " as its failure sentinel. A
    # user-provided script may legitimately quote that text anywhere, including
    # at the beginning, so only interpret it for an LLM-generated script.
    is_provider_error = (
        not (params.video_script or "").strip()
        and isinstance(video_script, str)
        and video_script.startswith("Error: ")
    )
    if not video_script or is_provider_error:
        rejected = script_quality.parse_failed_draft(video_script) if is_provider_error else None
        if rejected:
            rejected_draft, quality_issues = rejected
            manifest_path = path.join(utils.task_dir(task_id), "script.json")
            draft_manifest = {
                "script": rejected_draft,
                "rejected_script_draft": rejected_draft,
                "script_quality_accepted": False,
                "script_quality_issues": quality_issues,
            }
            if path.isfile(manifest_path):
                task_artifacts.patch_script_data(task_id, **draft_manifest)
            else:
                task_artifacts.write_script_data(task_id, draft_manifest)
            return _mark_task_failed(
                task_id, "script",
                "Savage narration was rejected after one bounded rewrite. "
                f"Issues: {'; '.join(quality_issues)}. Edit the saved draft before retrying.",
                details={"rejected_script_draft": rejected_draft,
                         "script_quality_issues": quality_issues},
            )
        error = (
            video_script.removeprefix("Error: ").strip()
            if is_provider_error
            else "failed to generate video script"
        )
        return _mark_task_failed(task_id, "script", error)

    effective_style = settings.effective_visual_style
    style_reason = settings.visual_style_reason or params.visual_style_reason
    params.visual_style_reason = style_reason
    if not reuse_saved_audio:
        params.video_script = video_script
        video_script = _verify_script_for_policy(task_id, params, video_script)
        if not video_script:
            return None
    if (stop_at in {"materials", "video"}
            and effective_style != visual_style.SAVAGE_ANIMATED_EXPLAINER):
        allowed_sources = (
            {"pexels", "pixabay", "local"}
            if effective_style == visual_style.ASMR_CUTTING
            else {"auto", "pexels", "pixabay", "coverr"}
        )
        if params.video_source not in allowed_sources:
            return _mark_task_failed(
                task_id, "preflight",
                f"{effective_style} requires a licensed stock source; choose one of "
                f"{', '.join(sorted(allowed_sources))}.",
                details={"requested_visual_style": params.visual_style,
                         "effective_visual_style": effective_style,
                         "visual_style_reason": style_reason},
            )
    logger.info(
        "visual style requested/effective: "
        f"requested={params.visual_style}, effective={effective_style}, reason={style_reason}"
    )

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, script=video_script
        )
        return {"script": video_script}

    # 2. Generate terms
    if not reuse_saved_audio:
        video_terms = ""
    if (not reuse_saved_audio and stop_at in {"terms", "materials", "video"}
            and params.video_source != "local"):
        video_terms = generate_terms(task_id, params, video_script)
        if not video_terms:
            return _mark_task_failed(
                task_id,
                "terms",
                "failed to generate video search terms",
            )

    if not reuse_saved_audio:
        save_script_data(task_id, video_script, video_terms, params)

    if stop_at == "terms":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, terms=video_terms
        )
        return {"script": video_script, "terms": video_terms}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20)

    # 3. Generate audio
    generate_audio_kwargs = {
        "voice_preview": voice_preview,
        "allow_server_file_input": allow_server_file_input,
    }
    if voxcpm_reference_audio is not None:
        generate_audio_kwargs["voxcpm_reference_audio"] = voxcpm_reference_audio
    if voxcpm_prompt_audio is not None:
        generate_audio_kwargs["voxcpm_prompt_audio"] = voxcpm_prompt_audio
        generate_audio_kwargs["voxcpm_prompt_text"] = voxcpm_prompt_text
    recovery_attempts = 0
    if reuse_saved_audio:
        audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
        actual_narration_duration = voice.get_audio_duration(audio_file)
        if actual_narration_duration <= 0:
            return _mark_task_failed(task_id, "resume", "saved narration audio is missing or invalid")
        audio_duration = math.ceil(actual_narration_duration)
        task_artifacts.cache_artifact(
            task_id, "audio", _narration_cache_key(params.spoken_script, settings, (None, None, b""), params=params),
            audio_file, duration=actual_narration_duration,
        )
        sub_maker = None
    else:
        while True:
            audio_file, audio_duration, sub_maker = generate_audio(
                task_id,
                params,
                video_script,
                **generate_audio_kwargs,
            )
            if not audio_file:
                return _mark_task_failed(
                    task_id,
                    "audio",
                    "failed to prepare narration audio",
                )
            actual_narration_duration = voice.get_audio_duration(audio_file)
            if actual_narration_duration <= 0:
                # generate_audio has already required a positive measured duration;
                # retain its rounded estimate only if a second probe is unavailable.
                actual_narration_duration = float(audio_duration or 0)
            if actual_narration_duration <= 0:
                return _mark_task_failed(task_id, "audio", "narration audio duration is zero")
            lower = params.min_duration_seconds
            upper = params.max_duration_seconds
            outside = _duration_outside_range(actual_narration_duration, lower, upper)
            if params.duration_enforcement != "strict" or not outside:
                if outside and params.duration_enforcement == "warn_only":
                    logger.warning(
                        "narration duration differs from requested range: task_id={}, "
                        "requested={}–{}s, actual={:.2f}s",
                        task_id, lower, upper, actual_narration_duration,
                    )
                break
            # Regenerate only when the caller explicitly enabled bounded duration recovery.
            can_recover = (
                not getattr(params, "custom_audio_file", None)
                and params.duration_recovery_approved
                and recovery_attempts < params.duration_recovery_attempts
            )
            if can_recover:
                revised_script = _revise_script_for_duration(
                    video_script, params, actual_narration_duration
                )
                if revised_script and revised_script != video_script:
                    recovery_attempts += 1
                    video_script = revised_script
                    params.video_script = revised_script
                    _clear_fact_verification(params)
                    video_script = _verify_script_for_policy(task_id, params, video_script)
                    if not video_script:
                        return None
                    video_terms = generate_terms(task_id, params, video_script)
                    if not video_terms:
                        return _mark_task_failed(task_id, "terms", "failed to refresh search terms after duration revision")
                    save_script_data(task_id, video_script, video_terms, params)
                    logger.info(f"duration recovery attempt {recovery_attempts}/{params.duration_recovery_attempts}")
                    continue
            return _mark_task_failed(
                task_id,
                "audio",
                f"actual narration is {actual_narration_duration:.2f}s, outside the requested "
                f"{lower}–{upper}s range",
                details={"actual_narration_duration_seconds": actual_narration_duration,
                         "min_duration_seconds": lower, "max_duration_seconds": upper,
                         "duration_recovery_attempts": recovery_attempts,
                         "duration_recovery_limit": params.duration_recovery_attempts},
            )

    params.actual_narration_duration_seconds = actual_narration_duration
    duration_metadata = _duration_metadata(params, narration=actual_narration_duration)
    params.duration_outside_requested_range = duration_metadata["duration_outside_requested_range"]
    task_artifacts.patch_script_data(task_id, duration_metadata=duration_metadata)
    sm.state.update_task(
        task_id, actual_narration_duration_seconds=actual_narration_duration,
        duration_metadata=duration_metadata,
    )

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)

    if stop_at == "audio":
        # Full video jobs apply gain in the final mixer. Audio-only exports have
        # no mixer, so apply it here to an owned output (never a custom input).
        audio_volume = 1.0 if params.voice_volume is None else float(params.voice_volume)
        if audio_volume != 1.0:
            descriptor, output_file = tempfile.mkstemp(
                prefix="audio-export-", suffix=".mp3", dir=utils.task_dir(task_id)
            )
            os.close(descriptor)
            export_ready = False
            try:
                export_ready = voice.apply_audio_volume(audio_file, output_file, audio_volume)
                if not export_ready:
                    return _mark_task_failed(task_id, "audio", "failed to apply audio export volume")
            finally:
                if not export_ready:
                    try:
                        os.remove(output_file)
                    except OSError as exc:
                        logger.warning(f"failed to remove incomplete audio export: {exc}")
            audio_file = output_file
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            audio_file=audio_file,
            **duration_metadata,
        )
        return {"audio_file": audio_file, "audio_duration": audio_duration,
                **duration_metadata}

    # 4. Generate subtitle
    if params.effective_visual_style == visual_style.SAVAGE_ANIMATED_EXPLAINER:
        params.subtitle_enabled = True
        params.subtitle_display_mode = "word_by_word"
    spoken_script = params.spoken_script or _represent_script(params, video_script).spoken_script
    subtitle_path = generate_subtitle(
        task_id, params, spoken_script, sub_maker, audio_file
    )

    if params.subtitle_enabled and not subtitle_path:
        return _mark_task_failed(
            task_id, "subtitle",
            "Enabled subtitles could not be aligned to the narration; "
            "review alignment-quality.json for every timing and confidence issue. "
            "The saved narration can be reused without TTS.",
        )

    if stop_at == "subtitle":
        if not subtitle_path:
            return _mark_task_failed(
                task_id,
                "subtitle",
                "failed to generate subtitles; verify the subtitle provider and timeline",
            )
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
            **duration_metadata,
        )
        return {"subtitle_path": subtitle_path, **duration_metadata}

    # Branding is optional and pinned in the immutable task/job snapshot. Keep
    # the original narration file and its alignment unchanged; build a separate
    # final track, then shift the narration cues by the measured intro duration.
    narration_subtitle_path = subtitle_path
    main_narration_duration = actual_narration_duration
    intro_duration = 0.0
    branding_video_segments = []
    branding = getattr(params, "channel_branding", {}) or {}
    channel_id = getattr(params, "channel_id", "")
    if channel_id and isinstance(branding, dict):
        from app.services import channel_branding as branding_service

        segments = []
        skipped = []
        for kind in ("intro", "outro"):
            item = branding.get(kind) or {}
            if not item.get("enabled"):
                continue
            asset = item.get("audio") or {}
            try:
                asset_path = branding_service.resolve_asset(
                    channel_id, asset.get("asset_key"), expected_kind=kind
                )
                expected_hash = asset.get("content_hash")
                if (not isinstance(expected_hash, str)
                        or hashlib.sha256(asset_path.read_bytes()).hexdigest() != expected_hash):
                    raise ValueError("branding audio failed its saved content-hash check")
                measured = voice.get_audio_duration(str(asset_path))
                if measured <= 0:
                    raise ValueError("branding audio has zero duration")
                cue_path = branding_service.read_subtitle(channel_id, asset, kind)
                segments.append((kind, str(asset_path), measured, cue_path))
                if not item.get("video"):
                    skipped.append({
                        "segment": f"{kind}_video",
                        "reason": "no saved branded video; audio will be used with the selected visuals",
                    })
            except (ValueError, OSError, TypeError) as exc:
                skipped.append({"segment": kind, "reason": str(exc)})

        if segments:
            ordered = ([entry for entry in segments if entry[0] == "intro"]
                       + [("narration", audio_file, actual_narration_duration, None)]
                       + [entry for entry in segments if entry[0] == "outro"])
            output_audio = path.join(utils.task_dir(task_id), "audio-with-branding.mp3")
            try:
                if not voice._concat_audio_files([entry[1] for entry in ordered], output_audio):
                    raise RuntimeError("audio assembly failed")
                assembled_duration = voice.get_audio_duration(output_audio)
                if assembled_duration <= 0:
                    raise RuntimeError("assembled audio has zero duration")
                intro_duration = sum(entry[2] for entry in ordered
                                     if entry[0] == "intro")
                elapsed_for_video = 0.0
                for kind, _audio_path, segment_duration, cue in ordered:
                    item = branding.get(kind) or {}
                    video_asset = item.get("video") or {}
                    if kind in {"intro", "outro"} and video_asset:
                        try:
                            if video_asset.get("layout_version") != 2:
                                raise ValueError("saved video uses an older layout; regenerate it to match the final avatar position")
                            video_path = branding_service.resolve_video(
                                channel_id, video_asset.get("asset_key"),
                                expected_kind=kind,
                            )
                            audio_hash = hashlib.sha256(Path(_audio_path).read_bytes()).hexdigest()
                            subtitle_hash = hashlib.sha256(Path(cue).read_bytes()).hexdigest() if cue else ""
                            if (audio_hash != video_asset.get("audio_hash")
                                    or subtitle_hash != video_asset.get("subtitle_hash")
                                    or hashlib.sha256(video_path.read_bytes()).hexdigest()
                                    != video_asset.get("content_hash")):
                                raise ValueError("branding video no longer matches its audio or subtitles")
                            avatar_hash = ""
                            profile_image = branding.get("profile_image") or {}
                            if kind == "outro" and profile_image:
                                avatar_path = branding_service.resolve_profile_image(
                                    channel_id, profile_image.get("asset_key"),
                                )
                                avatar_hash = hashlib.sha256(avatar_path.read_bytes()).hexdigest()
                            if avatar_hash != str(video_asset.get("avatar_hash") or ""):
                                raise ValueError("branding video no longer matches the channel profile image")
                            branding_video_segments.append({
                                "path": str(video_path), "start": elapsed_for_video,
                                "end": elapsed_for_video + segment_duration,
                                "sha256": video_asset["content_hash"],
                                "asset_key": video_asset["asset_key"],
                                "placement": "full_frame",
                            })
                        except (ValueError, OSError, TypeError, KeyError) as exc:
                            skipped.append({"segment": f"{kind}_video", "reason": str(exc)})
                    elapsed_for_video += segment_duration
                cue_blocks = []
                if subtitle_path and os.path.isfile(subtitle_path):
                    cue_blocks.append(branding_service.offset_srt(subtitle_path, intro_duration))
                elapsed = 0.0
                for kind, _, duration, cue in ordered:
                    if cue:
                        cue_blocks.append(branding_service.offset_srt(cue, elapsed))
                    elapsed += duration
                # Branding cues are independently authored subtitle assets.
                # They are merged when narration subtitles are disabled; an
                # enabled narration track must already have valid cues or the
                # pipeline stops before this branding/render stage.
                if cue_blocks:
                    merged_srt = path.join(utils.task_dir(task_id), "subtitle-with-branding.srt")
                    if _write_merged_subtitles(cue_blocks, merged_srt):
                        subtitle_path = merged_srt
                audio_file = output_audio
                # Normalize the assembled track as one unit so uploaded and
                # generated branding matches the main narration loudness.
                params.voice_loudness_target_lufs = -16.0
                actual_narration_duration = assembled_duration
                audio_duration = math.ceil(assembled_duration)
                params.actual_narration_duration_seconds = assembled_duration
                duration_metadata = _duration_metadata(params, narration=assembled_duration)
                params.duration_outside_requested_range = duration_metadata[
                    "duration_outside_requested_range"
                ]
                sm.state.update_task(
                    task_id,
                    actual_narration_duration_seconds=assembled_duration,
                    channel_branding_applied=[entry[0] for entry in segments],
                    channel_branding_video_applied=[
                        {key: segment.get(key) for key in ("asset_key", "sha256", "start", "end")}
                        for segment in branding_video_segments
                    ],
                    channel_branding_fallbacks=skipped,
                    duration_metadata=duration_metadata,
                )
                task_artifacts.patch_script_data(
                    task_id, channel_branding_applied=[entry[0] for entry in segments],
                    channel_branding_video_applied=[
                        {key: segment.get(key) for key in ("asset_key", "sha256", "start", "end")}
                        for segment in branding_video_segments
                    ],
                    channel_branding_fallbacks=skipped,
                    duration_metadata=duration_metadata,
                )
            except Exception as exc:
                logger.warning("channel branding skipped after assembly error: task_id={}, error={}",
                               task_id, type(exc).__name__)
                os.remove(output_audio) if os.path.exists(output_audio) else None
                sm.state.update_task(
                    task_id, channel_branding_applied=[],
                    channel_branding_fallbacks=skipped + [{
                        "segment": "assembly", "reason": "branding audio could not be assembled"
                    }],
                )
        elif skipped:
            sm.state.update_task(task_id, channel_branding_applied=[],
                                 channel_branding_fallbacks=skipped)

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=40)

    # 5. Get video materials
    try:
        if params.effective_visual_style == visual_style.SAVAGE_ANIMATED_EXPLAINER:
            downloaded_videos = [animated_explainer.render_background(
                task_id, params.video_subject, audio_duration,
                subtitle_path=narration_subtitle_path,
                spoken_script=spoken_script,
                narration_duration_seconds=main_narration_duration,
                narration_offset_seconds=intro_duration,
                emotion_beats=emotion_storyboard.build(
                    spoken_script,
                    narrator_preset=settings.narrator_preset,
                    profanity_level=settings.profanity_level,
                ),
                stock_provider=params.video_source,
                stock_terms=video_terms,
                caption_mode=params.subtitle_display_mode,
                stock_media_policy=params.stock_media_policy,
                branding_segments=branding_video_segments,
            )]
        else:
            downloaded_videos = get_video_materials(
                task_id,
                params,
                video_terms,
                audio_duration,
                loomloom_video_request=loomloom_video_request,
                raise_selection_errors=True,
            )
    except (material.NoRelevantAsmrMaterialsError,
            material.InsufficientRelevantStockError) as exc:
        return _mark_task_failed(
            task_id, "materials", str(exc),
            details={"requested_visual_style": params.visual_style,
                     "effective_visual_style": params.effective_visual_style,
                     "visual_style_reason": params.visual_style_reason},
        )
    if not downloaded_videos:
        return _mark_task_failed(
            task_id,
            "materials",
            "failed to prepare video materials",
        )

    if params.stock_video_style == asmr.SATISFYING_ASMR_CUTTING:
        try:
            with open(path.join(utils.task_dir(task_id), "script.json"), encoding="utf-8") as file:
                source_records = json.load(file).get("material_sources", [])
            matched = {
                record.get("local_file")
                for record in source_records
                if isinstance(record, dict)
                and record.get("provider") in {"pexels", "pixabay", "curated_asmr"}
                and record.get("asmr_relevance_score", 0) > 0
                and asmr.classify_asmr_term(record.get("search_term", ""))
                and (record.get("provider") != "curated_asmr"
                     or record.get("curated_reviewed") is True)
            }
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            logger.warning(f"cannot verify ASMR material provenance: {exc}")
            matched = set()
        if not downloaded_videos or any(path.basename(item) not in matched for item in downloaded_videos):
            return _mark_task_failed(
                task_id, "materials",
                "ASMR footage provenance could not be verified; refusing to render unrelated stock.",
            )

    if stop_at == "materials":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            materials=downloaded_videos,
        )
        return {"materials": downloaded_videos}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50)

    # 仅完整视频生成流程才需要处理视频拼接模式；
    # 这样可以避免 /subtitle 和 /audio 这类请求访问不存在的字段。
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)
    if params.effective_visual_style == visual_style.SAVAGE_ANIMATED_EXPLAINER:
        # Keep the timed explainer as one continuous scene. The final MP4 is
        # still trimmed to the measured narration duration by the normal muxer.
        params.video_concat_mode = VideoConcatMode.sequential
        params.video_transition_mode = None

    # 6. Generate final videos
    partial_results = {}
    try:
        final_video_paths, combined_video_paths, generation_warnings = generate_final_videos(
            task_id,
            params,
            downloaded_videos,
            audio_file,
            subtitle_path,
            audio_duration,
            partial_results=partial_results,
            branding_video_segments=branding_video_segments,
        )
    except Exception as exc:
        logger.exception(f"video batch failed, task_id: {task_id}, error: {exc}")
        # Warnings for the failing clip do not describe any usable output.
        completed_count = len(partial_results.get("videos", []))
        partial_results["warnings"] = [
            warning for warning in partial_results.get("warnings", [])
            if warning.get("video_index", 0) <= completed_count
        ] or None
        return _mark_task_failed(
            task_id, "video", f"{type(exc).__name__}: {exc}", details=partial_results,
        )

    if not final_video_paths:
        return _mark_task_failed(
            task_id,
            "video",
            "failed to generate final video",
        )

    youtube_metadata_by_video = _generate_completed_video_youtube_metadata(
        task_id,
        params,
        params.spoken_script or _represent_script(params, video_script).spoken_script,
        final_video_paths,
    )
    if youtube_metadata_by_video:
        task_artifacts.patch_script_data(
            task_id, youtube_metadata_by_video=youtube_metadata_by_video,
        )

    final_video_duration = _measure_video_duration(final_video_paths[0])
    params.actual_video_duration_seconds = final_video_duration
    duration_metadata = _duration_metadata(
        params, narration=actual_narration_duration, final_video=final_video_duration
    )
    params.duration_outside_requested_range = duration_metadata[
        "duration_outside_requested_range"
    ]
    if (duration_metadata["final_video_outside_requested_range"]
            and params.duration_enforcement == "warn_only"):
        logger.warning(
            "final video duration differs from requested range: task_id={}, "
            "requested={}–{}s, actual={:.2f}s",
            task_id, params.min_duration_seconds, params.max_duration_seconds,
            final_video_duration,
        )
    task_artifacts.patch_script_data(task_id, duration_metadata=duration_metadata)

    if params.effective_visual_style == visual_style.SAVAGE_ANIMATED_EXPLAINER:
        animated_explainer.cleanup_stock_media(task_id)
    elif params.effective_visual_style == visual_style.ASMR_CUTTING:
        removed_materials = set(
            material.cleanup_task_stock_materials(task_id, downloaded_videos)
        )
        downloaded_videos = [
            item for item in downloaded_videos if path.basename(item) not in removed_materials
        ]

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos."
    )

    # 7. 先完成视频生成任务，再按需提交跨平台发布。第三方上传可能耗时
    # 数分钟，不应阻塞视频结果返回，也不能反向影响已经生成的成片。
    cross_post_requested = (
        not suppress_auto_upload
        and upload_post.upload_post_service.is_configured()
        and upload_post.upload_post_service.auto_upload
    )
    youtube_privacy_status = upload_post.upload_post_service.youtube_privacy_status
    spoken_script = params.spoken_script or _represent_script(params, video_script).spoken_script
    expected_fact_hash = hashlib.sha256(spoken_script.encode("utf-8")).hexdigest()
    fact_verified = (
        params.fact_verification_state == "source_verified"
        and params.fact_verification_script_hash == expected_fact_hash
    )
    unverified_publication_label = (
        str(youtube_privacy_status).lower() == "public" and not fact_verified
    )
    unverified_publication_blocked = (
        cross_post_requested and unverified_publication_label
        and not params.allow_unverified_publication
    )
    cross_post_enabled = cross_post_requested and not unverified_publication_blocked
    platforms = list(upload_post.upload_post_service.platforms) if cross_post_enabled else []
    if unverified_publication_blocked:
        logger.warning(
            "automatic public cross-post skipped for unverified narration; task_id={}", task_id
        )
    should_cross_post = cross_post_enabled and bool(platforms)
    if cross_post_enabled and not platforms:
        logger.warning(
            f"skip cross-post because no platforms are configured, task_id: {task_id}"
        )
    cross_post_state = const.CROSS_POST_STATE_PENDING if should_cross_post else None

    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        **duration_metadata,
        "subtitle_path": subtitle_path,
        "materials": downloaded_videos,
        "youtube_metadata_by_video": youtube_metadata_by_video,
        "cross_post_state": cross_post_state,
        "cross_post_results": None,
        "cross_post_error": None,
        "cross_post_owner": _cross_post_process_owner if should_cross_post else None,
        "unverified_publication_blocked": unverified_publication_blocked,
        "warnings": generation_warnings or None,
        "visual_style_selection": {
            "requested_mode": params.visual_style,
            "effective_mode": params.effective_visual_style,
            "reason": params.visual_style_reason,
        },
    }
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )

    if should_cross_post:
        scheduling_error = _schedule_cross_post(
            task_id=task_id,
            video_paths=final_video_paths,
            params=params,
            video_script=video_script,
            platforms=platforms,
            youtube_privacy_status=youtube_privacy_status,
            # 固定排队时的受众选择，之后修改 WebUI 不应改变已排队视频的声明。
            youtube_made_for_kids=(
                upload_post.upload_post_service.youtube_made_for_kids
            ),
            unverified_publication_label=unverified_publication_label,
        )
        # 队列满或线程池关闭属于同步可知的调度失败。任务状态已经由调度函数
        # 更新，这里同步修正返回快照，避免调用方收到与后续查询不一致的 pending。
        if scheduling_error:
            kwargs["cross_post_state"] = const.CROSS_POST_STATE_FAILED
            kwargs["cross_post_error"] = scheduling_error
            kwargs["cross_post_owner"] = None

    return kwargs


def start(
    task_id,
    params: VideoParams,
    stop_at: str = "video",
    voice_preview: dict | None = None,
    loomloom_video_request: loomloom.LoomLoomConfirmedVideoRequest | None = None,
    allow_server_file_input: bool = False,
    voxcpm_reference_audio: bytes | None = None,
    voxcpm_prompt_audio: bytes | None = None,
    voxcpm_prompt_text: str = "",
    suppress_auto_upload: bool = False,
    reuse_saved_audio: bool = False,
):
    """
    执行任务流水线，并确保未预期异常也会转换成可查询的失败状态。

    ``allow_server_file_input`` 只供本机 CLI 使用。HTTP API 和 WebUI 必须保持
    默认值，让自定义音频始终受当前任务目录约束。
    """
    try:
        return _run_pipeline(
            task_id,
            params,
            stop_at=stop_at,
            voice_preview=voice_preview,
            loomloom_video_request=loomloom_video_request,
            allow_server_file_input=allow_server_file_input,
            voxcpm_reference_audio=voxcpm_reference_audio,
            voxcpm_prompt_audio=voxcpm_prompt_audio,
            voxcpm_prompt_text=voxcpm_prompt_text,
            suppress_auto_upload=suppress_auto_upload,
            reuse_saved_audio=reuse_saved_audio,
        )
    except Exception as exc:
        logger.exception(
            f"unexpected task pipeline failure, task_id: {task_id}, error: {exc}"
        )
        return _mark_task_failed(
            task_id,
            "pipeline",
            f"{type(exc).__name__}: {exc}",
        )


def resume_saved_audio_task(task_id: str):
    """Resume an audio-stage failure using its saved script and audio, without TTS."""
    task_root = path.realpath(utils.task_dir(task_id))
    manifest_file = path.join(task_root, "script.json")
    audio_file = path.join(task_root, "audio.mp3")
    if not path.isfile(manifest_file) or not path.isfile(audio_file):
        return _mark_task_failed(task_id, "resume", "saved script or narration audio is missing")
    try:
        with open(manifest_file, encoding="utf-8") as source:
            manifest = json.load(source)
        params_data = manifest.get("params")
        if not isinstance(params_data, dict):
            raise ValueError("saved task parameters are missing")
        params = VideoParams.model_validate(params_data)
        if params.effective_visual_style != visual_style.SAVAGE_ANIMATED_EXPLAINER:
            raise ValueError("safe saved-audio resume is currently limited to local animated explainer tasks")
        if bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume):
            if _VIDEO_MUSIC_PROVIDERS.get(params.bgm_type):
                raise ValueError("saved-audio resume cannot initiate a paid music provider")
        if not manifest.get("display_word_map"):
            raise ValueError("saved task is missing the display-to-alignment word map")
        if not manifest.get("spoken_script"):
            raise ValueError("saved task is missing the canonical spoken script")
    except (OSError, ValueError, TypeError) as exc:
        return _mark_task_failed(task_id, "resume", f"saved task cannot be resumed: {exc}")
    logger.info("resuming saved audio task without TTS: task_id={}", task_id)
    return start(
        task_id, params, stop_at="video", suppress_auto_upload=True,
        reuse_saved_audio=True,
    )


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
