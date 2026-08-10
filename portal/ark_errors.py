"""Chinese translations for Ark structured error responses.

Ark's video-generation endpoints (used by seedance and volcengine-portrait)
return failures as ``{"code": ..., "message": ...}`` in English. A raw dump of
that dict is what production users see when a job fails, and the actionable
sentence is buried in whatever `message` happens to say. This module owns the
translation table so the two sub-apps can share it — nano-banana talks to Ark
too but its error shape is different (OpenAI-compatible chat/completions), so
it doesn't consume this table today.

Design notes:

- Matcher order matters — first hit wins. Each entry is
  ``(code_prefix, message_substring_or_None, chinese_explanation)``:
    * ``code_prefix`` matches by ``startswith`` so a single entry can cover
      every ``InvalidParameter.*`` subcode.
    * ``message_substring`` narrows further when one code has many meanings
      (``InvalidParameter`` alone has four distinct causes in our logs).
      Pass ``None`` to match on code alone.

- When Ark adds a new error we haven't translated, ``translate_ark_error``
  returns ``None`` — the caller is expected to still show the raw
  ``code — message`` so the operator can find the Ark request id. A missing
  entry therefore degrades gracefully rather than swallowing the failure.

- Type annotations use ``Optional[str]`` (not ``str | None``) so this module
  can be imported from the pytest interpreter, which is Python 3.9 on the
  service machine even though the sub-app runtime is 3.12.

Sources for the initial table:
- grep of ``activity_log.json`` across all sub-apps (frequencies below)
- production incidents (2026-08-07 content-policy, 2026-08-10 4s edit)
- Ark 2.5 tutorial docs
"""

from __future__ import annotations

from typing import Optional, Tuple


ArkErrorMatcher = Tuple[str, Optional[str], str]


ARK_ERROR_MATCHERS: Tuple[ArkErrorMatcher, ...] = (
    # 18 hits — biggest one. Asset ref survived past its deletion, or was
    # referenced while still Processing.
    ("InvalidParameter", "is not found",
     "引用的素材在方舟侧不存在（可能刚上传还在处理，或已被删除）。请刷新素材列表后重新选择。"),

    # 1 hit — user uploaded a video into the reference-image slot. Browser
    # `accept="image/*"` is only a soft constraint.
    ("InvalidParameter", "is not an image",
     "上传的文件是视频或音频，但被放到了参考图槽位。请把它放到「参考视频」或「参考音频」槽位。"),

    # 6 hits — 2.0 series' 15-second ceiling. Ark reports the exact cap.
    ("InvalidParameter", "must be less than or equal to 15",
     "Seedance 2.0 系列最长支持 15 秒。如需生成更长的视频，请切到 Seedance 2.5 模型（支持 30 秒）。"),

    # 4 hits — duration outside the accepted set for the current model.
    ("InvalidParameter", "duration specified in the request is not valid",
     "时长参数不合法。Seedance 2.0 系列支持 4-15 秒；Seedance 2.5 支持 4-30 秒。"
     "视频编辑任务须填 -1（由模型自动决定）。"),

    # 1 hit — production incident 2026-08-10. Edit task needs a >=4s input.
    ("InvalidParameter.TaskTypeConstraint", "duration requirement of 4 to 30 seconds",
     "Seedance 2.5 判定为「视频编辑」任务，编辑输入的参考视频时长必须在 4~30 秒之间。"
     "当前上传的视频时长不足 4 秒，请换一个更长的参考视频；"
     "或如果不是想做编辑任务，请把提示词里「删除/编辑/替换/增加」等关键词去掉。"),

    # 1 hit (input) + known case (output). Content policy — the code names
    # which side triggered it, but the user-facing advice is the same.
    ("InputImageSensitiveContentDetected", None,
     "输入图片内容审核未通过（可能涉及版权/敏感形象）。请更换素材，或改写提示词避开相关描述。"),
    ("OutputVideoSensitiveContentDetected", None,
     "生成结果内容审核未通过（可能涉及版权/敏感形象）。请改写提示词，避开可能触发版权限制的表述。"),

    # Known incident 2026-08-07 — 2.5 was in the model list but not activated
    # on the account.
    ("ModelNotOpen", None,
     "该模型尚未在方舟控制台开通。Seedance 2.5 需要账号余额 ≥ 200 元或已购资源包，"
     "请在方舟控制台「视觉生成」页面开通后重试。"),

    # Typo / stale model id.
    ("InvalidEndpointOrModel.NotFound", None,
     "模型 ID 无效或账号无权访问。请联系管理员检查模型配置。"),
)


def translate_ark_error(code: str, message: str) -> Optional[str]:
    """Return a Chinese explanation for a matched Ark error, or None if no
    matcher fires. The caller is responsible for still surfacing the raw
    code/message so the operator can find the Ark request id."""
    for code_prefix, msg_sub, zh in ARK_ERROR_MATCHERS:
        if not code.startswith(code_prefix):
            continue
        if msg_sub is not None and msg_sub not in message:
            continue
        return zh
    return None
