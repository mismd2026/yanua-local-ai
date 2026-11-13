"""会話の流れ（LangGraph）。

- internal/ : 社内向け（helpdesk：システムの問い合わせ。今後 general_affairs：総務を追加）
- external/ : 社外向け（reception：受付。今後追加）

どちらに回すかは、入口（社内ポータルか、代表電話・HPか）で決める。LLMには判断させない。
検索・回答・記録は yanua の中心部分（search / answer / store）をそのまま使う。
"""
