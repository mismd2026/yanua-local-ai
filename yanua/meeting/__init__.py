"""打ち合わせの記録：録画・録音を社内で文字にし、話し手・要約・決まったこと・宿題をまとめて議事録にする。

- transcribe: 文字起こし（faster-whisper、この端末の中で動かす）
- speakers:   話し手（Zoom・Teams・Meet の録画に出ている名前を、画面から読み取る）
- summarize:  要約・決まったこと・宿題（社内のLLM）
- minutes:    議事録（Markdown と、会社の様式の Word）
"""
