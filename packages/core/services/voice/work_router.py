"""Local foreground routing while durable Voice Chat work is active."""

from __future__ import annotations

import re

from packages.core.services.voice.work_queue import is_voice_progress_query
from packages.core.services.voice.work_types import (
    VoiceControlReplyKind,
    VoiceLanguage,
    VoiceWorkAction,
    VoiceWorkContext,
    VoiceWorkDecision,
    VoiceWorkState,
)


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().casefold())


def _summary(text: str, limit: int = 48) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else f"{value[:limit].rstrip()}…"


def _reply(
    action: VoiceWorkAction,
    *,
    user_input: str,
    active_work: VoiceWorkContext,
) -> str:
    language = VoiceLanguage.detect(user_input)
    if action is VoiceWorkAction.QUEUE:
        active_request = _summary(active_work.request)
        queued_request = _summary(user_input)
        return {
            "zh": f"我会先继续“{active_request}”，完成后再处理“{queued_request}”。",
            "ko": f"먼저 ‘{active_request}’ 작업을 계속하고, 끝나면 ‘{queued_request}’ 요청을 처리할게요.",
            "ja": f"まず「{active_request}」を続け、完了したら「{queued_request}」を処理します。",
            "en": f"I'll finish “{active_request}” first, then handle “{queued_request}.”",
        }[language]
    if action is VoiceWorkAction.STATUS:
        partial = _summary(active_work.assistant_output, 72)
        request = _summary(active_work.request)
        if active_work.state is VoiceWorkState.COMPLETED:
            return {
                "zh": f"刚刚已经完成。{f'结果是：{partial}。' if partial else '结果已经发到对话里了。'}",
                "ko": f"방금 완료됐어요. {f'결과는 {partial}입니다.' if partial else '결과는 대화에 저장됐어요.'}",
                "ja": f"先ほど完了しました。{f'結果は「{partial}」です。' if partial else '結果はチャットに保存しました。'}",
                "en": f"It just finished. {f'The result is: {partial}.' if partial else 'The result is in the chat.'}",
            }[language]
        if active_work.state is VoiceWorkState.INTERRUPTED:
            return {
                "zh": "正在停止上一项。新要求会在它安全退出后开始。" if active_work.superseded_by else "停止请求已经提交。已执行的操作可能无法撤回。",
                "ko": "이전 작업을 중지 중이며 안전하게 끝나면 새 요청을 시작할게요." if active_work.superseded_by else "중지 요청을 보냈어요. 이미 실행된 작업은 되돌릴 수 없을 수 있어요.",
                "ja": "前のタスクを停止中です。安全に終了したら新しい依頼を開始します。" if active_work.superseded_by else "停止を要求しました。すでに実行された操作は元に戻せない場合があります。",
                "en": "I'm stopping the previous task. The new request will start after it exits safely." if active_work.superseded_by else "The stop request was sent. Completed actions may not be reversible.",
            }[language]
        if active_work.state is VoiceWorkState.FAILED:
            return {
                "zh": "刚才的任务没有完成。错误详情已经发到对话里了。",
                "ko": "방금 작업을 완료하지 못했어요. 오류 내용은 대화에 남겼어요.",
                "ja": "先ほどのタスクは完了できませんでした。エラーの詳細はチャットに保存しました。",
                "en": "That task did not finish. The error details are in the chat.",
            }[language]
        if language is VoiceLanguage.ZH:
            if partial:
                return f"目前的进展是：{partial}。任务还在继续，完成后我会告诉你。"
            return f"我还在处理“{request}”，完成后会告诉你。"
        if language is VoiceLanguage.KO:
            if partial:
                return f"현재 진행 내용은 {partial}입니다. 계속 처리 중이며 완료되면 알려드릴게요."
            return f"지금 ‘{request}’ 작업을 처리 중이에요. 완료되면 알려드릴게요."
        if language is VoiceLanguage.JA:
            if partial:
                return f"現在の進捗は「{partial}」です。処理を続け、完了したらお知らせします。"
            return f"「{request}」を処理中です。完了したらお知らせします。"
        if partial:
            return f"Current progress: {partial}. It is still running, and I'll tell you when it finishes."
        return f"I'm still working on “{request}”. I'll tell you when it finishes."
    if action is VoiceWorkAction.CANCEL:
        return {
            "zh": "好，我会停止当前任务。已经执行的操作可能无法撤回。",
            "ko": "알겠습니다. 현재 작업을 중지할게요. 이미 실행된 작업은 되돌릴 수 없을 수 있어요.",
            "ja": "現在のタスクを停止します。すでに実行された操作は元に戻せない場合があります。",
            "en": "Okay, I'll stop the current task. Actions already completed may not be reversible.",
        }[language]
    if action is VoiceWorkAction.REPLACE:
        if active_work.state is VoiceWorkState.COMPLETED:
            return {
                "zh": "上一项刚刚完成。我现在按你的新要求处理。",
                "ko": "이전 작업이 방금 끝났어요. 이제 새 요청을 처리할게요.",
                "ja": "前のタスクは先ほど完了しました。これから新しい依頼を処理します。",
                "en": "The previous task just finished. I'll start your new request now.",
            }[language]
        return {
            "zh": "好，我会停止上一项，按你刚才的新要求处理。已经执行的操作可能无法撤回。",
            "ko": "알겠습니다. 이전 작업을 중지하고 방금 요청으로 바꿀게요. 이미 실행된 작업은 되돌릴 수 없을 수 있어요.",
            "ja": "前のタスクを停止し、今の依頼に切り替えます。すでに実行された操作は元に戻せない場合があります。",
            "en": "Okay, I'll stop the previous task and switch to your new request. Completed actions may not be reversible.",
        }[language]
    if action is VoiceWorkAction.CLARIFY:
        return {
            "zh": "你要停止当前任务，还是让它继续完成？",
            "ko": "현재 작업을 중지할까요, 아니면 계속 완료할까요?",
            "ja": "現在のタスクを停止しますか、それとも続けますか？",
            "en": "Should I stop the current task, or let it finish?",
        }[language]
    return ""


_CANCEL_PATTERNS = (
    r"(?:先)?(?:停|停下|停止|取消|别做|不要做|不用做)(?:一下|这个|那个|它|上一(?:个|项)|刚才(?:那个|的任务)|当前任务)?(?:了|吧|先)?[。.!！?？]*",
    r"(?:please )?(?:stop|cancel)(?: it| that| this| the task| the current task)?(?: now| please)?[.!?]*",
    r"(?:그만|중지|취소)(?:해|하세요|해줘|해 주세요)?[.!?]*",
    r"(?:止めて|停止して|キャンセルして)(?:ください)?[。.!?]*",
)
_REPLACE_PATTERNS = (
    r"(?:别|不要|不用).{0,80}(?:了[，, ]*)?(?:改成|改为|换成|换为|转而).+",
    r"(?:停止|取消|停(?:一下|下)?|忽略).{0,80}(?:改成|改为|换成|换为|然后|转而).+",
    r"(?:不对|不是这个).{0,40}(?:改成|改为|应该|我要).+",
    r"(?:stop|cancel|ignore|forget).{0,80}(?:instead|then|and instead).+",
    r"(?:stop|cancel) (?:it|that|this|the task)[,.]? (?:and )?(?:do|run|open|send|check|create|write) .+",
    r"(?:no[,.]? )?(?:change|switch) (?:it|that|that task|the task) to .+",
    r"(?:do|use|make) .+ instead[.!?]*",
    r"(?:그만|중지|취소).{0,80}(?:대신|바꿔).+",
    r"(?:止め|停止|キャンセル).{0,80}(?:代わりに|変更して).+",
)
_AMBIGUOUS_CORRECTION_PATTERNS = (
    r"(?:不对|不是这个|搞错了|做错了)[。.!！?？]*",
    r"(?:that's wrong|not that|wrong task)[.!?]*",
    r"(?:그게 아니야|잘못됐어)[.!?]*",
    r"(?:違う|それじゃない)[。.!?]*",
)
_RESULT_STATUS_PATTERNS = (
    r"(?:现在)?(?:做到|处理到|查到)(?:哪里|哪儿|哪一步|什么阶段)(?:了)?[吗呢]?[。.!！?？]*",
    r"(?:查到|找到|拿到|有)(?:什么|啥|结果|消息)(?:了)?[吗呢]?[。.!！?？]*",
    r"(?:what|anything) (?:did you|have you) (?:find|learn|get)[?!.]*",
    r"(?:what(?:'s| is) the result|show me what you have)[?!.]*",
    r"(?:何か|結果は).*(?:分かった|見つかった|ありますか)[。.!?]*",
    r"(?:뭘|무엇을|결과를).*(?:찾았|알았|얻었)[어나요습니까?!.]*",
)


class VoiceWorkReplyFactory:
    """Build localized foreground replies from typed routing decisions."""

    _CONTROL_REPLIES = {
        VoiceLanguage.ZH: {
            VoiceControlReplyKind.QUEUED: "已经记下，上一项完成后继续。",
            VoiceControlReplyKind.PROGRESS: "还在处理中，完成后我会马上告诉你。",
            VoiceControlReplyKind.COMPLETED: "刚刚已经完成，结果也发到对话里了。",
            VoiceControlReplyKind.ERROR: "处理时遇到了问题，请稍后再试。",
            VoiceControlReplyKind.SILENCE: "",
            VoiceControlReplyKind.LANGUAGE: "抱歉，刚才的语言识别错了。我会跟随你现在使用的语言。",
            VoiceControlReplyKind.CONFUSED: "抱歉，刚才的播报不清楚。我会保持简短。",
            VoiceControlReplyKind.IDLE: "现在没有正在执行的任务。",
        },
        VoiceLanguage.KO: {
            VoiceControlReplyKind.QUEUED: "기록했어요. 이전 작업이 끝나면 이어서 할게요.",
            VoiceControlReplyKind.PROGRESS: "아직 처리 중이에요. 끝나는 대로 바로 알려드릴게요.",
            VoiceControlReplyKind.COMPLETED: "방금 완료했고 결과도 대화에 남겼어요.",
            VoiceControlReplyKind.ERROR: "처리 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.",
            VoiceControlReplyKind.SILENCE: "",
            VoiceControlReplyKind.LANGUAGE: "죄송해요. 방금 언어를 잘못 인식했어요. 지금 사용하시는 언어로 답할게요.",
            VoiceControlReplyKind.CONFUSED: "죄송해요. 방금 안내가 불분명했어요. 짧고 명확하게 말할게요.",
            VoiceControlReplyKind.IDLE: "지금 실행 중인 작업은 없어요.",
        },
        VoiceLanguage.JA: {
            VoiceControlReplyKind.QUEUED: "承知しました。前のタスクが終わり次第続けます。",
            VoiceControlReplyKind.PROGRESS: "まだ処理中です。終わり次第すぐにお知らせします。",
            VoiceControlReplyKind.COMPLETED: "先ほど完了し、結果もチャットに保存しました。",
            VoiceControlReplyKind.ERROR: "処理中に問題が発生しました。少し後でもう一度お試しください。",
            VoiceControlReplyKind.SILENCE: "",
            VoiceControlReplyKind.LANGUAGE: "すみません。言語の認識を誤りました。今お使いの言語で答えます。",
            VoiceControlReplyKind.CONFUSED: "すみません。先ほどの案内が不明瞭でした。短く明確に話します。",
            VoiceControlReplyKind.IDLE: "現在実行中のタスクはありません。",
        },
        VoiceLanguage.EN: {
            VoiceControlReplyKind.QUEUED: "Got it. I'll continue with that after the current task.",
            VoiceControlReplyKind.PROGRESS: "I'm still working on it. I'll tell you as soon as it's ready.",
            VoiceControlReplyKind.COMPLETED: "It just finished, and I added the result to the chat.",
            VoiceControlReplyKind.ERROR: "I ran into a problem while working on that. Please try again shortly.",
            VoiceControlReplyKind.SILENCE: "",
            VoiceControlReplyKind.LANGUAGE: "Sorry, I detected the wrong language. I'll use the language you're using now.",
            VoiceControlReplyKind.CONFUSED: "Sorry, that was unclear. I'll keep it brief.",
            VoiceControlReplyKind.IDLE: "There isn't a task running right now.",
        },
    }

    def create(
        self,
        action: VoiceWorkAction,
        *,
        user_input: str,
        active_work: VoiceWorkContext,
    ) -> str:
        return _reply(
            action,
            user_input=user_input,
            active_work=active_work,
        )

    def create_control(
        self,
        user_input: str,
        kind: VoiceControlReplyKind,
    ) -> str:
        return self._CONTROL_REPLIES[VoiceLanguage.detect(user_input)][kind]


class VoiceWorkDecisionFactory:
    """Create one conservative decision from input and durable work context."""

    def __init__(self, reply_factory: VoiceWorkReplyFactory | None = None) -> None:
        self.reply_factory = reply_factory or VoiceWorkReplyFactory()

    @staticmethod
    def _matches(text: str, patterns: tuple[str, ...]) -> bool:
        return any(
            re.fullmatch(pattern, text, re.IGNORECASE)
            for pattern in patterns
        )

    def create(
        self,
        user_input: str,
        active_work: VoiceWorkContext,
        requested_action: VoiceWorkAction | None = None,
    ) -> VoiceWorkDecision:
        text = _normalized(user_input)
        if not text:
            return VoiceWorkDecision(VoiceWorkAction.QUEUE)
        if requested_action is not None:
            action = requested_action
        elif is_voice_progress_query(text) or self._matches(
            text,
            _RESULT_STATUS_PATTERNS,
        ):
            action = VoiceWorkAction.STATUS
        elif self._matches(text, _REPLACE_PATTERNS):
            action = VoiceWorkAction.REPLACE
        elif self._matches(text, _CANCEL_PATTERNS):
            action = VoiceWorkAction.CANCEL
        elif self._matches(text, _AMBIGUOUS_CORRECTION_PATTERNS):
            action = VoiceWorkAction.CLARIFY
        else:
            action = VoiceWorkAction.QUEUE
        if (
            active_work.state is VoiceWorkState.COMPLETED
            and action in {VoiceWorkAction.CANCEL, VoiceWorkAction.CLARIFY}
        ):
            action = VoiceWorkAction.STATUS
        return VoiceWorkDecision(
            action=action,
            reply=self.reply_factory.create(
                action,
                user_input=user_input,
                active_work=active_work,
            ),
            superseded_by=active_work.superseded_by,
        )


voice_work_reply_factory = VoiceWorkReplyFactory()
voice_work_decision_factory = VoiceWorkDecisionFactory(voice_work_reply_factory)


def classify_voice_work_followup(
    user_input: str,
    active_work: VoiceWorkContext,
    requested_action: VoiceWorkAction | None = None,
) -> VoiceWorkDecision:
    """Compatibility entrypoint for the foreground decision factory."""

    return voice_work_decision_factory.create(
        user_input,
        active_work,
        requested_action=requested_action,
    )
