"""拼进 system 的各块大标题。

system 消息是把好几块内容拼起来的（原则、人设、表情说明、时间提示、记忆、摘要……）。
每块前面给一个大标题，模型才知道这一段是什么、该按什么权重对待。

* :meth:`app.conversation.service.ConversationService._system_prompt` —— 【基本原则】/【角色设定】
* :meth:`app.conversation.service.ConversationService._runtime_notes` —— 表情标记的说明，作为运行期说明传给下面
* :meth:`app.conversation.memory.MemoryStore.build_context` —— 【标记说明】（表情标记 + 当前时间）/【被打断】/【长期记忆】/【相关记忆】/【过往摘要】

"""

from __future__ import annotations

# ---- 说话约束与人设（service._system_prompt）--------------------------------
#: 全局基础提示词（设置 → 模式设置 → 基础提示词）
BASIC = "【基本原则】"
#: 角色自己的提示词（设置 → 角色设置 → 角色提示词）
PERSONA = "【角色设定】"

# ---- 运行期注入的说明（memory.build_context）--------------------------------
#: 表情标记与当前时间（时间）的说明合成同一节：
#: 两块内容各按自己的条件决定要不要出现（自动表情开关 / 本次是否带当前时间）。
MARKS = "【标记说明】"
#: 被打断标记的说明，只在上下文里真带着被打断的回复时出现
INTERRUPT = "【被打断】"

# ---- 记忆相关（memory.build_context）----------------------------------------
#: 长期稳定的事实与偏好
PROFILE = "【长期记忆】"
#: 本次按话题检索出来的记忆
RECALL = "【相关记忆】"
#: 更早对话的滚动摘要
SUMMARY = "【过往摘要】"
