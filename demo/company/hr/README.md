# 人事データのサンプル（架空）

人事システムから出力したファイルを取り込む例です。人物はすべて架空です。

- `hr_export_sample.csv`：人事システムの出力の例。本人確認に使わない列（氏名カナ・生年月日・入社日）も含まれています
- `mapping_sample.json`：列名を Yanua の項目に読み替える対応表

```bash
# 対応表の下書きを作る（列名だけから作る。中身は読まない）
python -m yanua hr-mapping-draft demo/company/hr/hr_export_sample.csv

# 取り込む（対応表にない列は読み込んだ時点で捨てる。毎回まるごと入れ替える）
python -m yanua hr-import demo/company/hr/hr_export_sample.csv --mapping demo/company/hr/mapping_sample.json
```

在籍区分が「在籍」「休職」の人だけが本人確認で一致します。「退職」の人は一致しません。
