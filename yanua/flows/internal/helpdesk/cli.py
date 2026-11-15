"""ヘルプデスクをコマンドラインで動かす（デモと対話モード）。"""

from __future__ import annotations

import json
import uuid
from typing import Callable

from langgraph.types import Command

# デモの返事：質問文に含まれる言葉で、どの返事を使うかを決める（聞き返しの回数が変わってもずれない）
DEMOS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "1": ("eigyo-common", "代表アカウント／プリンター／社内文書で案内、解決せず票を作成", [
        ("本人確認を行う都合上", "営業部の高橋美咲です。番号は10004です。"),
        ("お困り", "プリンターから印刷できません"),
        ("状況を確認", "本社2階の営業部の複合機です。エラー表示は出ていません。部署の全員が印刷できず、業務が止まっています。"),
        ("あと少し", "本社2階の複合機です。部署の全員が使えず、業務が止まっています。"),
        ("解決しましたか", "いいえ"),
        ("連絡先", "内線4100、9時から18時"),
    ]),
    "2": ("yamada.t", "個人アカウント（SSO）／ネットワーク／障害中の案内で終了", [
        ("よろしいですか", "はい"),
        ("お困り", "インターネットにつながりません"),
        ("状況を確認", "本社3階の自席で、有線です。周りの人もつながらず、仕事が止まっています。"),
        ("あと少し", "本社3階、有線LANです。部署の全員がつながらず、業務が止まっています。"),
    ]),
    "3": ("soumu-common", "代表アカウント／人事データに見当たらず（新人など）／不審なメールで必ず引き継ぎ", [
        ("本人確認を行う都合上", "総務部の山本次郎です。番号は99999です。"),
        ("お困り", "不審なメールの添付ファイルを開いてしまいました"),
        ("連絡先", "内線2201、終日"),
        ("わかる範囲", "今朝9時ごろ添付を開きました。パスワードは入力していません。"),
    ]),
    "4": ("tanaka.k", "個人アカウントを別の人が使用／番号なしで名前と部署から確定／パスワード忘れ／社内文書で案内して解決", [
        ("よろしいですか", "いいえ"),
        ("本人確認を行う都合上", "総務の佐藤花子です。番号はわかりません。"),
        ("お困り", "パスワードを忘れました"),
        ("解決しましたか", "はい"),
    ]),
}


def run(app, account: str, answer_for: Callable[[str], str], echo: bool = False, out=print) -> dict:
    """1回の問い合わせを最後まで進める。返事待ちのたびに answer_for(質問) で返事を得る。"""
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    state = app.invoke({"account": account}, config)
    while "__interrupt__" in state:
        question = state["__interrupt__"][0].value
        out(f"\nAI  > {question}")
        answer = answer_for(question)
        if echo:
            out(f"利用者> {answer}")
        state = app.invoke(Command(resume=answer), config)
    out(f"\nAI  > {state['result']}")
    if state.get("ticket"):
        out("\n■ 作成した問い合わせ票")
        out(json.dumps(state["ticket"], ensure_ascii=False, indent=2))
    return state


def demo_answers(key: str) -> tuple[str, str, Callable[[str], str]]:
    account, title, answers = DEMOS[key]

    def answer_for(q: str) -> str:
        for word, ans in answers:
            if word in q:
                return ans
        return input("利用者> ")

    return account, title, answer_for
