"""Extension contracts. No implicit network credentials or external sending.

App-internal delivery currently uses persistent task history and reminder_due audit.
Calendar providers must independently obtain user consent and implement this contract.
APNs requires signed app entitlement + device registration + provider credentials.
Never connect these adapters directly to model output; use preview/approval/audit first.
"""
from typing import Protocol, List, Dict

class CalendarAdapter(Protocol):
    def list_events(self, start: str, end: str) -> List[Dict]: ...
    def preview_event(self, title: str, when: str) -> Dict: ...
    def commit_approved(self, preview: Dict, approval_id: str) -> str: ...
    def undo(self, event_id: str, approval_id: str) -> None: ...

class DeliveryAdapter(Protocol):
    def deliver(self, task_id: str, text: str, recipient: str) -> str: ...

class UnconfiguredAdapter:
    def deliver(self, *args, **kwargs):
        raise RuntimeError('未配置通知渠道；结果已保存在 App 内任务历史。微信个人号自动发送不在 MVP 范围。')
