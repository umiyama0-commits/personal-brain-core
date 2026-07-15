"""_looks_like_correction の module-level 在席 + 訂正検出ロジックの regression guard.

★2026-06-10: 旧版は _handle_lineworks_message 内の nested 定義だったため、別の
module-level 関数 _maybe_capture_conversation_continuation から呼ぶと NameError →
except で握りつぶされ、会話継続 = positive-signal の記録が silent fail していた。
module-level へ昇格する修正の再発防止。

main.py は god object (import が重く副作用あり) なので、ast で当該関数 + 依存定数
だけを抽出して exec する (= import せず pure ロジックだけ回す)。
"""
import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "main.py"


def _module_level_funcs():
    tree = ast.parse(MAIN.read_text())
    return [n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _load_looks_like_correction():
    """main.py を import せず、_looks_like_correction と依存定数だけ抽出して返す."""
    tree = ast.parse(MAIN.read_text())
    ns: dict = {}
    for n in tree.body:
        is_const = (isinstance(n, ast.Assign)
                    and any(getattr(t, "id", "").startswith("_CORRECTION")
                            for t in n.targets))
        is_func = isinstance(n, ast.FunctionDef) and n.name == "_looks_like_correction"
        if is_const or is_func:
            exec(compile(ast.Module(body=[n], type_ignores=[]), str(MAIN), "exec"), ns)
    return ns["_looks_like_correction"]


def test_is_module_level():
    """nested ではなく module-level に定義されていること (NameError regression guard)."""
    assert "_looks_like_correction" in _module_level_funcs()


def test_detects_corrections():
    f = _load_looks_like_correction()
    for t in ["違うよ", "それは違う", "事実誤認だ", "古い情報です", "正しくは3月だ",
              "間違ってると思う", "事実と違う"]:
        assert f(t) is True, t


def test_passes_normal_messages():
    f = _load_looks_like_correction()
    for t in ["ありがとう", "今日の売上は?", "了解です", "a", "x" * 401]:
        assert f(t) is False, t
