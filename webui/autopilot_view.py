"""Presentation helpers for Channel Autopilot."""

from app.services.autopilot import normalize_content
from app.services import visual_style


def migrate_create_video_style(state, labels, values, version=2):
    """Upgrade an old default while retaining an explicit choice/restored task."""
    if state.get("_visual_style_defaults_version") == version:
        return False
    if not state.get("_visual_style_user_selected", False):
        old_style = values.get(state.get("visual_style_select"), "auto")
        state["visual_style_select"] = labels[visual_style.SAVAGE_ANIMATED_EXPLAINER]
        state["_active_visual_style_preset"] = old_style
        state["_apply_style_defaults_before_render"] = True
    state["_visual_style_defaults_version"] = version
    return True


def sync_manual_style_state(
    state, channel_id, profile_version, profile_style, styles,
    profile_duration=30, profile_aspect="9:16",
):
    """Default manual creation to Savage and retain later per-video style edits."""
    key = f"simple_style_{channel_id}"
    version_key = f"simple_style_profile_version_{channel_id}"
    defaults_version_key = f"simple_style_defaults_version_{channel_id}"
    defaults_version = 1
    active_key = f"simple_active_style_{channel_id}"
    previous_profile_style = state.get(f"simple_profile_style_{channel_id}")
    selected_before = styles.get(state.get(key), previous_profile_style or profile_style)
    if state.get(defaults_version_key) != defaults_version:
        if not state.get(f"simple_style_user_selected_{channel_id}", False):
            selected_default = visual_style.SAVAGE_ANIMATED_EXPLAINER
            state[key] = next(
                label for label, value in styles.items() if value == selected_default
            )
        state[defaults_version_key] = defaults_version
        state[active_key] = selected_before
    elif previous_profile_style != profile_style and selected_before == previous_profile_style:
        state[key] = next(label for label, value in styles.items() if value == profile_style)
    state[version_key] = profile_version
    state[f"simple_profile_style_{channel_id}"] = profile_style

    selected_style = styles.get(state.get(key), profile_style)
    if state.get(active_key, profile_style) == selected_style:
        return
    defaults = visual_style.preset_for(selected_style).defaults
    narrator_labels = {
        "standard": "Standard",
        "desi_savage_aggressive": "Desi Savage Hindi",
    }
    profanity_labels = {
        "mild": "Mild",
        "savage_monetization_aware": "Savage — Monetization Aware",
        "savage_unfiltered": "Savage Unfiltered",
    }
    if defaults.get("narrator_preset") in narrator_labels:
        state[f"simple_narrator_{channel_id}"] = narrator_labels[defaults["narrator_preset"]]
    if defaults.get("profanity_level") in profanity_labels:
        state[f"simple_profanity_{channel_id}"] = profanity_labels[defaults["profanity_level"]]
    if "min_duration_seconds" in defaults:
        state[f"simple_duration_{channel_id}"] = int(defaults["max_duration_seconds"])
    else:
        state[f"simple_duration_{channel_id}"] = int(profile_duration)
    state[f"simple_format_{channel_id}"] = (
        "Shorts (9:16)" if selected_style == visual_style.SAVAGE_ANIMATED_EXPLAINER
        else "Landscape (16:9)" if profile_aspect == "16:9"
        else "Shorts (9:16)"
    )
    if state.get(f"simple_draft_{channel_id}"):
        state[f"simple_script_needs_style_review_{channel_id}"] = True
    state[active_key] = selected_style


def reset_manual_style_defaults(state, channel_id, selected_style):
    """Reset narration controls to the current channel style preset."""
    defaults = visual_style.preset_for(selected_style).defaults
    narrator_labels = {
        "standard": "Standard",
        "desi_savage_aggressive": "Desi Savage Hindi",
    }
    profanity_labels = {
        "mild": "Mild",
        "savage_monetization_aware": "Savage — Monetization Aware",
        "savage_unfiltered": "Savage Unfiltered",
    }
    state[f"simple_narrator_{channel_id}"] = narrator_labels[defaults["narrator_preset"]]
    state[f"simple_profanity_{channel_id}"] = profanity_labels[defaults["profanity_level"]]
    state[f"simple_active_style_{channel_id}"] = selected_style
    if state.get(f"simple_draft_{channel_id}"):
        state[f"simple_script_needs_style_review_{channel_id}"] = True


def group_video_attempts(jobs):
    """Group manual retries in newest-first order, keeping destinations distinct."""
    groups = {}
    for job in sorted(jobs, key=lambda item: item.created_at, reverse=True):
        profile = job.profile_snapshot
        if (getattr(job, "parent_job_id", None)
                or profile.get("publishing_policy") != "manual" or job.intended_publication_slot):
            key = (job.job_id,)
        else:
            key = (job.channel_id, normalize_content(job.topic), normalize_content(job.script),
                   *(profile.get(field) for field in (
                       "publishing_backend", "youtube_connection_id", "youtube_channel_id",
                       "upload_post_username")))
        groups.setdefault(key, []).append(job)
    return list(groups.values())
