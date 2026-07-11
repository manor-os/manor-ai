"""Internationalization — simple key-based translations.

Supports: en, zh, es, ja (extensible).
Usage:
    from packages.core.i18n import t, set_locale, get_locale

    set_locale("zh")
    msg = t("error.not_found")  # "未找到"
"""
from __future__ import annotations

import contextvars
from typing import Optional

_locale_var: contextvars.ContextVar[str] = contextvars.ContextVar("locale", default="en")

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "error.not_found": "Not found",
        "error.unauthorized": "Authentication required",
        "error.forbidden": "Permission denied",
        "error.rate_limited": "Too many requests, please try again later",
        "error.validation": "Validation error",
        "error.internal": "Internal server error",
        "task.created": "Task created successfully",
        "task.updated": "Task updated",
        "task.deleted": "Task deleted",
        "task.status_changed": "Task status changed to {status}",
        "doc.uploaded": "Document uploaded successfully",
        "doc.deleted": "Document deleted",
        "agent.created": "Agent created",
        "agent.subscribed": "Agent subscribed",
        "auth.registered": "Registration successful",
        "auth.login_success": "Login successful",
        "auth.password_changed": "Password changed successfully",
        "auth.reset_sent": "If this email exists, a reset link has been sent",
        "notification.none": "No notifications",
        "notification.marked_read": "Marked as read",
        "share.created": "Share link created",
        "share.revoked": "Share link revoked",
        "share.expired": "This share link has expired",
    },
    "zh": {
        "error.not_found": "未找到",
        "error.unauthorized": "需要身份验证",
        "error.forbidden": "权限不足",
        "error.rate_limited": "请求过于频繁，请稍后再试",
        "error.validation": "验证错误",
        "error.internal": "内部服务器错误",
        "task.created": "任务创建成功",
        "task.updated": "任务已更新",
        "task.deleted": "任务已删除",
        "task.status_changed": "任务状态已更改为 {status}",
        "doc.uploaded": "文档上传成功",
        "doc.deleted": "文档已删除",
        "agent.created": "智能体已创建",
        "agent.subscribed": "已订阅智能体",
        "auth.registered": "注册成功",
        "auth.login_success": "登录成功",
        "auth.password_changed": "密码修改成功",
        "auth.reset_sent": "如果该邮箱存在，重置链接已发送",
        "notification.none": "暂无通知",
        "notification.marked_read": "已标记为已读",
        "share.created": "分享链接已创建",
        "share.revoked": "分享链接已撤销",
        "share.expired": "此分享链接已过期",
    },
    "es": {
        "error.not_found": "No encontrado",
        "error.unauthorized": "Se requiere autenticación",
        "error.forbidden": "Permiso denegado",
        "error.rate_limited": "Demasiadas solicitudes, inténtelo más tarde",
        "error.validation": "Error de validación",
        "error.internal": "Error interno del servidor",
        "task.created": "Tarea creada con éxito",
        "task.updated": "Tarea actualizada",
        "task.deleted": "Tarea eliminada",
        "task.status_changed": "Estado de tarea cambiado a {status}",
        "doc.uploaded": "Documento subido con éxito",
        "doc.deleted": "Documento eliminado",
        "agent.created": "Agente creado",
        "auth.registered": "Registro exitoso",
        "auth.login_success": "Inicio de sesión exitoso",
        "auth.password_changed": "Contraseña cambiada con éxito",
    },
    "ja": {
        "error.not_found": "見つかりません",
        "error.unauthorized": "認証が必要です",
        "error.forbidden": "アクセス権限がありません",
        "task.created": "タスクが作成されました",
        "doc.uploaded": "ドキュメントがアップロードされました",
        "auth.registered": "登録が完了しました",
        "auth.login_success": "ログイン成功",
    },
}

SUPPORTED_LOCALES = set(TRANSLATIONS.keys())


def set_locale(locale: str) -> None:
    """Set the current request's locale (thread/task-local)."""
    _locale_var.set(locale if locale in SUPPORTED_LOCALES else "en")


def get_locale() -> str:
    """Return the current request's locale."""
    return _locale_var.get()


def t(key: str, locale: Optional[str] = None, **kwargs: object) -> str:
    """Translate a key. Falls back to English, then to the key itself."""
    loc = locale or get_locale()
    msg = TRANSLATIONS.get(loc, {}).get(key) or TRANSLATIONS["en"].get(key) or key
    if kwargs:
        try:
            msg = msg.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return msg
