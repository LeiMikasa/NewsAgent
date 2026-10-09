"""交互入口收集资料，Runtime 负责持久化；不在构造函数中调用 input。"""

from .memory import PROFILE_CHAR_LIMIT
from .runtime import Runtime

QUESTIONS = {
    "identity": "你的职业或角色是什么？可以补充背景和 AI 知识水平：",
    "preferences": "你关注哪些 AI 主题？对新闻来源、语言、深度和简报长度有什么偏好：",
}


def ensure_profile(runtime: Runtime, *, edit: bool = False, input_fn=None, output_fn=None) -> bool:
    """Only ask missing fields. Edit mode preserves existing fields on blank input.

    /skip or EOF cancels without writing partial answers. Returns whether saved.
    """
    input_fn = input_fn if input_fn is not None else input
    output_fn = output_fn if output_fn is not None else print
    if not runtime.memory.enabled:
        output_fn("长期记忆已关闭。")
        return False
    fields = list(QUESTIONS) if edit else runtime.missing_profile_fields
    if not fields:
        return False
    output_fn("先了解你的身份和阅读偏好，保存后下次可直接使用。输入 /skip 可跳过本次设置。")
    current = runtime.memory.profile()
    changes = {}
    for field_name in fields:
        if edit and current[field_name]:
            output_fn(f"当前{'身份' if field_name == 'identity' else '偏好'}：{current[field_name]}")
        while True:
            question = QUESTIONS[field_name] + ("（留空保留现有内容）" if edit and current[field_name] else "")
            try:
                value = input_fn(question).strip()
            except EOFError:
                runtime.ctx.extra["profile_setup_skipped"] = True
                output_fn("设置已跳过，现有资料保持不变。")
                return False
            if value in {"/skip", "/exit"}:
                runtime.ctx.extra["profile_setup_skipped"] = True
                output_fn("设置已跳过，现有资料保持不变。")
                return False
            if not value:
                if edit and current[field_name]:
                    break
                output_fn("请填写内容，或输入 /skip。")
                continue
            if len(value) > PROFILE_CHAR_LIMIT:
                output_fn(f"内容最多 {PROFILE_CHAR_LIMIT} 个字符，请缩短后重试。")
                continue
            changes[field_name] = value
            break
    if not changes:
        return False
    runtime.update_profile(**changes)
    output_fn("身份与订阅偏好已保存。")
    return True


def show_profile(runtime: Runtime, output_fn=None) -> None:
    output_fn = output_fn if output_fn is not None else print
    if not runtime.memory.enabled:
        output_fn("长期记忆已关闭。")
        return
    profile = runtime.memory.profile()
    output_fn("身份：" + (profile["identity"] or "未设置"))
    output_fn("订阅偏好：" + (profile["preferences"] or "未设置"))
    output_fn("输入 /profile edit 可修改；也可以在对话中明确说明长期变更。")
