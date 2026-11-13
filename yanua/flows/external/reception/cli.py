"""受付をコマンドラインで動かす（デモと対話モード）。"""

from __future__ import annotations

import json
import uuid
from typing import Callable

from langgraph.types import Command

DEMO_TODAY = "2026-11-16"   # visits.csv の来訪予定の日

# デモの返事：質問文に含まれる言葉で、どの返事を使うかを決める
DEMOS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "1": ("来訪", "お約束のある来訪／担当者に知らせて会議室へ案内", [
        ("ご用件", "ABC商事の木村です。営業部の高橋さんと15時にお約束しています。"),
        ("会社名", "ABC商事"), ("お名前", "木村 誠"),
    ]),
    "2": ("電話", "問い合わせ／社外公開の文書で答えて解決", [
        ("ご用件", "御社の営業時間と、最寄り駅からの行き方を教えてください。"),
        ("お役に立て", "はい"),
    ]),
    "3": ("Web", "クレーム／決まったおわびで聞き取り、お客様相談室へ（優先度：高）", [
        ("ご用件", "注文した商品がまだ届かない。もう2週間も待っている。"),
        ("どのようなこと", "11月1日にWebで注文したプリンター用トナーです。注文番号はA-1234です。"),
        ("お名前と", "小林 由美、090-1111-2222"),
    ]),
    "4": ("来訪", "お約束のない営業／決まった返事で資料を預かる", [
        ("ご用件", "新しい複合機のご提案で伺いました。"),
        ("会社名", "XYZ事務機"), ("お名前", "中村 翔"),
    ]),
    "5": ("電話", "社内の情報を聞かれる／社外公開の文書にないので答えず、折り返しの連絡先を聞く", [
        ("ご用件", "御社の人事評価の基準と、社員の皆さんの評価の結果を教えてください。"),
        ("お名前と", "加藤 学、03-2222-3333"),
    ]),
}


def run(app, channel: str, answer_for: Callable[[str], str], echo: bool = False, out=print) -> dict:
    """1回の受付を最後まで進める。返事待ちのたびに answer_for(質問) で返事を得る。"""
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    state = app.invoke({"channel": channel}, config)
    while "__interrupt__" in state:
        question = state["__interrupt__"][0].value
        out(f"\nAI    > {question}")
        answer = answer_for(question)
        if echo:
            out(f"お客様> {answer}")
        state = app.invoke(Command(resume=answer), config)
    out(f"\nAI    > {state['result']}")
    if state.get("record"):
        out("\n■ 社内向けの記録（お客様には見せない）")
        out(json.dumps(state["record"], ensure_ascii=False, indent=2))
    return state


def demo_answers(key: str) -> tuple[str, str, Callable[[str], str]]:
    channel, title, answers = DEMOS[key]

    def answer_for(q: str) -> str:
        for word, ans in answers:
            if word in q:
                return ans
        return input("お客様> ")

    return channel, title, answer_for
