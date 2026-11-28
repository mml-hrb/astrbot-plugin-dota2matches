"""隔离执行真实回退错误分支，不依赖 AstrBot 宿主。"""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


class ReportFallbackTests(unittest.TestCase):
    def test_fallback_error_combinations(self):
        tree = ast.parse(Path(__file__).with_name("main.py").read_text(encoding="utf-8"))
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_call_report_llm")
        handler = next(n for n in ast.walk(method) if isinstance(n, ast.ExceptHandler) and any(isinstance(s, ast.Return) for s in n.body))
        # 分支最后 return None 只适用于函数体；用同步包装器执行同一 AST。
        wrapper = ast.FunctionDef(name="run", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), body=handler.body, decorator_list=[])
        code = compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "<fallback-handler>", "exec")
        for prior in (None, SimpleNamespace(kind="other")):
            for label in ("", "辅助核验"):
                with self.subTest(prior=prior, label=label):
                    noted, warnings = [], []
                    env = dict(last_error=prior, label=label, e=RuntimeError("fallback"), self=SimpleNamespace(_note_report_error=noted.append), logger=SimpleNamespace(error=lambda *a, **k: None, warning=warnings.append), LLMRequestError=lambda message, **kw: RuntimeError(message))
                    exec(code, env)
                    self.assertIsNone(env["run"]())
                    self.assertEqual(len(noted), int(prior is None and not label))
                    self.assertEqual(len(warnings), int(prior is not None))


if __name__ == "__main__":
    unittest.main()
