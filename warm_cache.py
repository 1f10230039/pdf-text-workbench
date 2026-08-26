# -*- coding: utf-8 -*-
"""1冊ぶんの解析キャッシュを作る（→ cachekit.warm_doc）。

使い方:
    python warm_cache.py 企業名_年度 [企業名_年度 ...]

ui/app.py のジョブがこれを**サブプロセスとして並列に**走らせる。
extract_doc は Python 側の処理が多く GIL に縛られるので、スレッドでは速くならない。
プロセスを分ければコアの数だけ並ぶ（起動の約1秒は、1冊数十秒の解析に対して誤差）。
置き場は環境変数 WORKBENCH_DATA で渡せる（公開デモは一時ディレクトリを使うため）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cachekit


def main(names):
    if not names:
        print("使い方: python warm_cache.py 企業名_年度 [...]", file=sys.stderr)
        return 2
    code = 0
    for name in names:
        try:
            r = cachekit.warm_doc(name)
            print(json.dumps(r, ensure_ascii=False))
        except Exception as e:
            print(json.dumps({"name": name, "error": str(e)}, ensure_ascii=False),
                  file=sys.stderr)
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
