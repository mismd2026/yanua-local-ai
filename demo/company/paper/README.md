# 紙の書類のサンプル（架空）

複合機でスキャンしたPDFの代わりに使う、架空の書類です。社名・人名・金額はすべて架空です。

| ファイル | 内容 |
|---|---|
| `inbox/scan_0001.pdf` | 請求書（文字あり・表あり） |
| `inbox/scan_0002.pdf` | 見積書（文字あり・表あり） |
| `inbox/scan_0003.pdf` | 議事録（文字あり） |
| `inbox/scan_0004.pdf` | 納品書（画像だけ。OCRで読む） |
| `inbox/scan_0005.pdf` | 休業日のご案内（どのルールにも当たらない。ルールの下書き作りの見本） |
| `rules.json` | 仕分けのルール（請求書・見積書・納品書・議事録）。請求書・見積書・納品書には様式が付いている |
| `templates/` | 会社の様式の例（請求書.xlsx・請求書.docx・見積書.xlsx・納品書.docx）。差し込む位置を `{{...}}` で書いてある |
| `make_templates.py` | 上の様式を作り直すスクリプト |
| `make_samples.py` | 上のPDFを作り直すスクリプト（`pip install -r requirements-dev.txt` で reportlab と img2pdf が入る） |
